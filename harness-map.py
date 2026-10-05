#!/usr/bin/env python3
"""harness-map — Claude Code 하네스를 읽어 '조립 지도' JSON 으로 뽑는다.

읽기만 한다. 비밀값은 남기지 않는다:
  - env, MCP 설정은 키 이름·종류만 (값·인자·헤더 제외)
  - 훅 명령은 실행 파일 이름만 (인자·경로 제외)
  - 지시 파일은 경로·줄 수만 (본문 제외)

사용:
  python3 harness-map.py [--cwd 폴더 ...] [-o 출력.json] [--days 30] [--exclude-session ID] [--probes probes.json]
  python3 harness-map.py --probe-plan   (에이전트 몫 점검 안내 출력)
  --cwd 를 여러 번 주면 시작 폴더별 조립 결과를 함께 담는다 (기본: 현재 폴더).

권한: Claude Code 를 쓰는 그 사용자로 실행한다. 스크립트가 읽을 수 있는 범위 = Claude Code 가 읽는 범위다.
root 로 돌리면 홈이 바뀌어 남의 하네스를 읽는다(거부함). 읽기 권한 없는 파일은 unreadable 에 따로 남긴다.
스코프는 시작 폴더에서 위로 정해지므로, 위 폴더에서 한 번 돌리는 것으로는 아래 프로젝트 설정이 안 잡힌다.
"""
from __future__ import annotations

import argparse
import ast
import glob
import json
import operator
import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
CLAUDE = HOME / ".claude"
MANAGED_DIR = Path("/Library/Application Support/ClaudeCode") if os.uname().sysname == "Darwin" else Path("/etc/claude-code")

# 스폰 경계 상속 규칙. 공식 문서(code.claude.com/docs/en/sub-agents, agent-teams, memory) 2026-10-02 확인본.
# 값: "carry"(따라감) / "cut"(끊김) / "reload"(새 세션으로 다시 읽음) / "cond"(조건부) / "na"
SPAWN_RULES_DOCS = "code.claude.com/docs/en/{sub-agents,agent-teams,memory}.md · 2026-10-02 확인 · Claude Code 2.1.287"
SPAWN_RULES = {
    "main":      {"history": "carry", "claude_md": "carry",  "rules": "carry",  "auto_memory": "carry", "output_style": "carry", "skills": "carry",  "mcp": "carry",  "hooks": "carry", "perm_rules": "carry", "perm_mode": "carry", "model": "carry"},
    "fork":      {"history": "carry", "claude_md": "carry",  "rules": "carry",  "auto_memory": "carry", "output_style": "carry", "skills": "carry",  "mcp": "carry",  "hooks": "carry", "perm_rules": "carry", "perm_mode": "carry", "model": "carry"},
    "subagent":  {"history": "cut",   "claude_md": "reload", "rules": "reload",   "auto_memory": "cut",   "output_style": "cut",   "skills": "cond",   "mcp": "carry",  "hooks": "carry", "perm_rules": "carry", "perm_mode": "cond",  "model": "cond"},
    "teammate":  {"history": "cut",   "claude_md": "reload", "rules": "reload", "auto_memory": "reload","output_style": "cut",   "skills": "reload", "mcp": "reload", "hooks": "carry", "perm_rules": "carry", "perm_mode": "cond",  "model": "cond"},
    "headless":  {"history": "cut",   "claude_md": "reload", "rules": "reload", "auto_memory": "reload","output_style": "reload","skills": "reload", "mcp": "reload", "hooks": "carry", "perm_rules": "carry", "perm_mode": "cond",  "model": "cond"},
}
SPAWN_NOTES = {
    "fork": "포크는 부모 세션의 대화 기록, 시스템 프롬프트, 도구, 모델을 그대로 받는다. 작업이 끝나면 결과 요약만 부모 세션으로 돌아온다.",
    "subagent": "서브에이전트는 새 컨텍스트에서 시작한다. CLAUDE.md 와 rules 는 메인 세션과 같은 범위의 파일을 다시 읽는다. 다만 내장 Explore·Plan 에이전트와 정의에 omitClaudeMd: true 가 있는 에이전트는 CLAUDE.md 를 읽지 않는다. 자동 기억, 출력 스타일, 대화 기록은 전달되지 않는다. 권한 모드는 부모 세션이 bypassPermissions, acceptEdits, auto 중 하나일 때만 부모의 모드를 받는다. 스킬은 다시 호출할 수 있지만, 부모가 이미 불러온 스킬의 내용은 전달되지 않는다. 메인 세션 아래로 3단계까지 띄울 수 있고, 동시에 20개까지 실행된다.",
    "teammate": "팀원은 독립된 세션으로 실행된다. 프로젝트의 CLAUDE.md, MCP 서버, 스킬을 새로 읽는다. 리드 세션의 대화 기록은 전달되지 않고, 띄울 때 받은 지시문만 받는다. 권한 모드는 리드 세션의 모드를 받는다. 다만 dontAsk 모드는 전달되지 않고, bypassPermissions 모드는 모든 팀원에게 전달된다. 팀원은 다른 팀원을 띄울 수 없다. 팀원의 권한 요청은 리드 세션 화면에 표시된다.",
    "headless": "claude -p 로 실행한 세션은 대화 없이 새로 시작한다. 같은 폴더에서 실행하면 같은 설정 파일을 읽는다. 권한 모드는 --permission-mode 인자로 정한다.",
}


UNREADABLE: list[str] = []  # 존재하지만 이 사용자가 못 읽는 파일 = Claude Code 도 못 읽는 파일


def lines_of(p: Path) -> int:
    try:
        return sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
    except PermissionError:
        UNREADABLE.append(str(p))
        return 0
    except OSError:
        return 0


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except PermissionError:
        UNREADABLE.append(str(p))
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def frontmatter(p: Path) -> dict:
    """아주 단순한 YAML 머리말 파서 (name/description/model/tools/paths 정도만)."""
    try:
        t = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---", t, re.S)
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip().strip('"\'')
            out[key] = val if val else []
        elif key and re.match(r"^\s+-\s+", line):
            if not isinstance(out.get(key), list):
                out[key] = []
            out[key].append(line.split("-", 1)[1].strip().strip('"\''))
    return out


def cmd_name(command: str) -> str:
    """훅 명령에서 실행 파일·스크립트 이름만 남긴다."""
    toks = [t.strip("\"'") for t in re.split(r"\s+", command.strip()) if t]
    script = next((t for t in toks if re.search(r"\.(py|js|mjs|sh|ts)$", t)), None)
    base = os.path.basename((script or (toks[0] if toks else "?")).replace("${CLAUDE_PLUGIN_ROOT}", ""))
    return base[:60]


def hooks_from(hooks: dict, scope: str, source: str) -> list[dict]:
    out = []
    for event, groups in (hooks or {}).items():
        for g in groups or []:
            for h in g.get("hooks", []):
                out.append({"event": event, "scope": scope, "source": source, "matcher": g.get("matcher", "") or "*",
                            "type": h.get("type", "command"), "name": cmd_name(h.get("command", h.get("prompt", "")))[:60],
                            "blocking": not h.get("async", False)})
    return out


def git_root(d: Path) -> Path | None:
    try:
        r = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        return Path(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def project_slug(p: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


# ---------------------------------------------------------------- 플러그인
def plugin_dir(pid: str) -> Path | None:
    name, _, market = pid.partition("@")
    base = CLAUDE / "plugins" / "cache" / market / name
    if not base.exists():
        return None
    vers = [d for d in base.iterdir() if d.is_dir()]
    return max(vers, key=lambda d: d.stat().st_mtime) if vers else None


def read_plugin(pid: str) -> dict:
    d = plugin_dir(pid)
    info = {"id": pid, "version": d.name if d else None, "hooks": [], "agents": [], "skills": [], "commands": 0, "mcp": []}
    if not d:
        return info
    hj = read_json(d / "hooks" / "hooks.json")
    info["hooks"] = hooks_from(hj.get("hooks", {}), "plugin", pid)
    for a in sorted((d / "agents").glob("*.md")):
        fm = frontmatter(a)
        info["agents"].append({"name": fm.get("name") or a.stem, "model": fm.get("model") or "inherit"})
    info["skills"] = sorted(s.parent.name for s in (d / "skills").glob("*/SKILL.md"))
    info["commands"] = len(list((d / "commands").glob("*.md")))
    pj = read_json(d / ".claude-plugin" / "plugin.json")
    mcp = pj.get("mcpServers") if isinstance(pj.get("mcpServers"), dict) else read_json(d / ".mcp.json").get("mcpServers", {})
    info["mcp"] = sorted((mcp or {}).keys())
    return info


# ---------------------------------------------------------------- 스코프별 설정
def settings_layer(p: Path, scope: str) -> dict:
    s = read_json(p)
    if not s:
        return {}
    perm = s.get("permissions", {}) or {}
    return {"scope": scope, "path": str(p), "defaultMode": perm.get("defaultMode"),
            "allow": len(perm.get("allow", []) or []), "deny": len(perm.get("deny", []) or []), "ask": len(perm.get("ask", []) or []),
            "env_keys": sorted((s.get("env") or {}).keys()),
            "hooks": hooks_from(s.get("hooks", {}), scope, f"{scope} settings"),
            "sandbox": bool((s.get("sandbox") or {}).get("enabled")),
            "enabledPlugins": [k for k, v in (s.get("enabledPlugins") or {}).items() if v],
            "outputStyle": s.get("outputStyle"), "model": s.get("model"),
            "autoMemoryEnabled": s.get("autoMemoryEnabled")}


def md_files(scope: str, paths: list[Path], when: str) -> list[dict]:
    return [{"scope": scope, "path": str(p), "lines": lines_of(p), "load": when} for p in paths if p.is_file()]


def rules_in(d: Path, scope: str) -> list[dict]:
    out = []
    for r in sorted(d.rglob("*.md")) if d.is_dir() else []:
        paths = frontmatter(r).get("paths")
        out.append({"scope": scope, "path": str(r), "lines": lines_of(r),
                    "load": "on-read" if paths else "launch", "paths": paths if isinstance(paths, list) else ([paths] if paths else [])})
    return out


def agents_in(d: Path, scope: str) -> list[dict]:
    out = []
    for a in sorted(d.glob("*.md")) if d.is_dir() else []:
        fm = frontmatter(a)
        out.append({"scope": scope, "name": fm.get("name") or a.stem, "model": fm.get("model") or "inherit",
                    "omitClaudeMd": fm.get("omitClaudeMd"), "has_memory": bool(fm.get("memory")),
                    "tools_restricted": bool(fm.get("tools") or fm.get("disallowedTools")),
                    "permissionMode": fm.get("permissionMode"), "skills": fm.get("skills") or None, "path": str(a)})
    return out


def assemble(cwd: Path, user_layer: dict, managed_layer: dict, plugins: list[dict], claude_json: dict) -> dict:
    """한 시작 폴더에서 조립되는 하네스."""
    root = git_root(cwd)
    ancestors = [cwd, *cwd.parents]
    ancestors = [a for a in ancestors if a != Path("/") and a != HOME.parent][::-1]  # 위에서 아래로

    # 맥락층: CLAUDE.md 체인
    claude_md = md_files("managed", [MANAGED_DIR / "CLAUDE.md"], "launch")
    claude_md += md_files("user", [CLAUDE / "CLAUDE.md"], "launch")
    for a in ancestors:
        if a == HOME:
            continue
        sc = "project" if (root and str(a).startswith(str(root))) or a == cwd else "ancestor"
        claude_md += md_files(sc, [a / "CLAUDE.md", a / ".claude" / "CLAUDE.md"], "launch")
        claude_md += md_files("local", [a / "CLAUDE.local.md"], "launch")
    # 아래 폴더의 CLAUDE.md 는 그 폴더 파일을 읽을 때 늦게 들어온다 (얕게 2단계만 센다)
    lazy = [p for p in list(cwd.glob("*/CLAUDE.md")) + list(cwd.glob("*/*/CLAUDE.md")) if "node_modules" not in str(p)]
    claude_md += md_files("subdir", lazy[:40], "on-read")

    rules = rules_in(CLAUDE / "rules", "user") + rules_in(cwd / ".claude" / "rules", "project")

    # auto memory: git 저장소 단위, 밖이면 시작 폴더 단위
    mem_root = root or cwd
    mem_dir = CLAUDE / "projects" / project_slug(mem_root) / "memory"
    mem_files = [p for p in mem_dir.glob("*.md") if p.name != "MEMORY.md"] if mem_dir.exists() else []
    auto_memory = {"keyed_by": "git 저장소" if root else "시작 폴더 (git 밖)", "root": str(mem_root), "dir": str(mem_dir),
                   "files": len(mem_files), "index_lines": lines_of(mem_dir / "MEMORY.md")}

    # 프로젝트 설정 계층
    proj_layer = settings_layer(cwd / ".claude" / "settings.json", "project")
    local_layer = settings_layer(cwd / ".claude" / "settings.local.json", "local")
    layers = [l for l in [user_layer, proj_layer, local_layer, managed_layer] if l]  # 낮은 → 높은 우선순위

    # 덮어쓰기 규칙: 뒤(높은 우선순위)가 이김
    def winner(key):
        val, src = None, None
        for l in layers:
            if l.get(key) is not None:
                val, src = l[key], l["scope"]
        return {"value": val, "from": src}

    enabled = set()
    for l in layers:
        enabled |= set(l.get("enabledPlugins", []))
    active_plugins = [p for p in plugins if p["id"] in enabled]

    hooks = [h for l in layers for h in l.get("hooks", [])] + [h for p in active_plugins for h in p["hooks"]]

    # 도구층: MCP
    mcp = [{"name": n, "scope": "user", "source": "~/.claude.json"} for n in sorted((claude_json.get("mcpServers") or {}).keys())]
    pj = (claude_json.get("projects") or {}).get(str(cwd), {})
    mcp += [{"name": n, "scope": "local", "source": "~/.claude.json projects[cwd]"} for n in sorted((pj.get("mcpServers") or {}).keys())]
    mcp += [{"name": n, "scope": "project", "source": ".mcp.json"} for n in sorted(read_json(cwd / ".mcp.json").get("mcpServers", {}).keys())]
    mcp += [{"name": n, "scope": "plugin", "source": p["id"]} for p in active_plugins for n in p["mcp"]]

    # 스킬
    skills = [{"name": s.parent.name, "scope": "user"} for s in sorted((CLAUDE / "skills").glob("*/SKILL.md"))]
    skills += [{"name": s.parent.name, "scope": "project"} for s in sorted((cwd / ".claude" / "skills").glob("*/SKILL.md"))]
    skills += [{"name": f"{p['id'].split('@')[0]}:{s}", "scope": "plugin"} for p in active_plugins for s in p["skills"]]

    # 에이전트 정의: 같은 이름이면 managed > project > user > plugin (CLI 인자는 정적으로 알 수 없음)
    agents = agents_in(MANAGED_DIR / "agents", "managed") + agents_in(cwd / ".claude" / "agents", "project") + agents_in(CLAUDE / "agents", "user")
    agents += [{"scope": "plugin", "name": f"{p['id'].split('@')[0]}:{a['name']}", "model": a["model"], "source": p["id"]} for p in active_plugins for a in p["agents"]]
    seen = {}
    for a in agents:
        a["shadowed_by"] = seen.get(a["name"])
        seen.setdefault(a["name"], a["scope"])

    env_keys = sorted({k for l in layers for k in l.get("env_keys", [])})
    mode = winner("defaultMode")
    teams_on = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in env_keys

    warnings = []
    if teams_on and mode["value"] in ("auto", "bypassPermissions", "acceptEdits"):
        warnings.append({"id": "teams-inherit-mode", "level": "high",
                         "text": f"팀 기능이 켜져 있고 기본 모드가 {mode['value']} — 이름 붙은 서브에이전트가 팀원으로 뜨고, 팀원·서브에이전트 모두 이 모드를 물려받는다."})
    if not root:
        warnings.append({"id": "memory-split", "level": "mid",
                         "text": "git 밖 폴더라 auto memory 가 이 폴더 기준으로 따로 생긴다. 위·아래 폴더에서 시작한 세션과 기억이 갈라진다."})
    by_event = {}
    for h in hooks:
        by_event.setdefault(h["event"], set()).add(h["source"])
    for ev, srcs in by_event.items():
        if len(srcs) >= 3:
            warnings.append({"id": f"hook-stack-{ev}", "level": "low",
                             "text": f"{ev} 에 출처 {len(srcs)}곳의 훅이 쌓여 있다 (모두 실행되고 순서는 보장되지 않는다)."})
    if sum(1 for c in claude_md if c["load"] == "launch") >= 3:
        warnings.append({"id": "md-concat", "level": "low",
                         "text": "시작 시 여러 CLAUDE.md 가 이어붙여진다. 서로 충돌하면 우선순위가 없고 모델이 판단한다."})
    if mode["value"] != "bypassPermissions" and mode["value"] not in ("auto", "acceptEdits"):
        warnings.append({"id": "subagent-mode-own", "level": "info",
                         "text": "부모 모드가 auto·acceptEdits·bypass 가 아니라 서브에이전트는 자기 permissionMode 를 쓴다."})
    if not proj_layer and not local_layer:
        warnings.append({"id": "global-only", "level": "info",
                         "text": "이 폴더에는 프로젝트·로컬 설정이 없다. 집행층은 전부 전역에서 온다."})

    return {
        "cwd": str(cwd), "git_root": str(root) if root else None,
        "context": {"claude_md": claude_md, "rules": rules, "auto_memory": auto_memory,
                    "output_style": winner("outputStyle"), "skills": skills},
        "tools": {"mcp": mcp},
        "enforcement": {"mode": mode, "model": winner("model"),
                        "perm": [{"scope": l["scope"], "allow": l["allow"], "deny": l["deny"], "ask": l["ask"]} for l in layers],
                        "hooks": hooks, "sandbox": any(l.get("sandbox") for l in layers), "env_keys": env_keys,
                        "layers": [l["scope"] for l in layers]},
        "agents": agents,
        "plugins": [{k: p[k] for k in ("id", "version")} | {"hooks": len(p["hooks"]), "agents": len(p["agents"]), "skills": len(p["skills"]),
                     "commands": p["commands"], "mcp": len(p["mcp"])} for p in active_plugins],
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 성숙도 채점
# 채점표 원본: 하네스 지도 개념 모델(명제 P1~P5, 암묵지 T1~T11, 케이스 1~7, 사실표 §1~§4)에서 도출한 6개 축.
# 각 레벨의 evidence 는 그 기준이 어느 명제·사실에서 왔는지를 가리킨다.
# 원본은 _workspace/04_maturity_model.md 6장의 JSON 블록(maturity-3)이며 그대로 옮겼다.
# 스크립트는 자동 레벨(레벨 4까지), runner == script 점검, 최종 레벨·필요 레벨을 계산한다.
# 에이전트 점검(runner == agent)은 자리만 만들고 --probes 로 받은 결과를 합쳐 다시 계산한다.
SCORECARD = json.loads(r'''{
 "version": "maturity-3",
 "hook_sets": {
  "PRE": [
   "PreToolUse",
   "PermissionRequest"
  ],
  "POST": [
   "PostToolUse",
   "PostToolUseFailure",
   "Stop",
   "StopFailure",
   "SubagentStop",
   "SessionEnd",
   "Notification"
  ],
  "CONTEXT": [
   "SessionStart",
   "UserPromptSubmit",
   "PreCompact"
  ]
 },
 "metrics": {
  "claude_md_launch_lines": {
   "from": "L.context.claude_md[]",
   "calc": "sum(lines where load == 'launch')"
  },
  "claude_md_scopes": {
   "from": "L.context.claude_md[]",
   "calc": "count(distinct scope where load == 'launch')"
  },
  "rules_count": {
   "from": "L.context.rules[]",
   "calc": "len"
  },
  "own_skills": {
   "from": "L.context.skills[]",
   "calc": "count(scope in {user, project})"
  },
  "memory_files": {
   "from": "L.context.auto_memory.files",
   "calc": "value"
  },
  "memory_index_lines": {
   "from": "L.context.auto_memory.index_lines",
   "calc": "value"
  },
  "in_git": {
   "from": "L.git_root",
   "calc": "1 if not null else 0"
  },
  "agent_memory": {
   "from": "L.agents[]",
   "calc": "count(scope in {user, project} and has_memory == true)"
  },
  "mcp_count": {
   "from": "L.tools.mcp[]",
   "calc": "len"
  },
  "plugin_count": {
   "from": "L.plugins[]",
   "calc": "len"
  },
  "folder_mcp": {
   "from": "L.tools.mcp[]",
   "calc": "count(scope in {local, project})"
  },
  "mcp_name_dupes": {
   "from": "L.tools.mcp[]",
   "calc": "len(name) - len(distinct name)"
  },
  "agents_tool_restricted": {
   "from": "L.agents[].tools_restricted",
   "calc": "count(== true); null if no agent has the key",
   "needs_extractor": true
  },
  "mode": {
   "from": "L.enforcement.mode.value",
   "calc": "value (string)"
  },
  "deny_total": {
   "from": "L.enforcement.perm[]",
   "calc": "sum(deny)"
  },
  "ask_total": {
   "from": "L.enforcement.perm[]",
   "calc": "sum(ask)"
  },
  "sandbox": {
   "from": "L.enforcement.sandbox",
   "calc": "1 if true else 0"
  },
  "pre_hooks_targeted": {
   "from": "L.enforcement.hooks[]",
   "calc": "count(event == 'PreToolUse' and matcher != '*')"
  },
  "post_hooks": {
   "from": "L.enforcement.hooks[]",
   "calc": "count(event in POST)"
  },
  "posttooluse_hooks": {
   "from": "L.enforcement.hooks[]",
   "calc": "count(event == 'PostToolUse')"
  },
  "stop_hooks": {
   "from": "L.enforcement.hooks[]",
   "calc": "count(event == 'Stop')"
  },
  "subagentstop_hooks": {
   "from": "L.enforcement.hooks[]",
   "calc": "count(event == 'SubagentStop')"
  },
  "own_agents": {
   "from": "L.agents[]",
   "calc": "count(scope in {user, project})"
  },
  "own_agent_models": {
   "from": "L.agents[]",
   "calc": "count(distinct model where scope in {user, project} and model not in {null, 'inherit'})"
  },
  "boundary_explicit": {
   "from": "L.agents[]",
   "calc": "count(scope in {user, project} and (omitClaudeMd != null or permissionMode != null or skills != null)); missing keys count as null"
  },
  "agents_shadowed": {
   "from": "L.agents[]",
   "calc": "count(shadowed_by != null)"
  },
  "warn_teams_mode": {
   "from": "L.warnings[]",
   "calc": "1 if any(id == 'teams-inherit-mode') else 0"
  },
  "claude_md_on_read": {
   "from": "L.context.claude_md[]",
   "calc": "count(load == 'on-read')"
  }
 },
 "aggregate": "lower_median",
 "auto_cap": 4,
 "axes": [
  {
   "id": "instruction",
   "name": "지시 설계",
   "summary": "모델에게 읽히는 지시 파일과 스킬이 범위와 시점에 맞게 나뉘어, 스폰된 에이전트에게도 필요한 지시가 도착하는 정도.",
   "evidence": [
    "P1",
    "P2",
    "P4",
    "T1",
    "T2",
    "T10",
    "case1",
    "case5",
    "facts§1",
    "facts§2",
    "facts§3",
    "facts§4",
    "arch§5"
   ],
   "levels": [
    {
     "level": 1,
     "name": "지시 없음",
     "desc": "시작 폴더와 그 위 폴더 어디에도 시작 시 읽히는 지시 파일이 없다.",
     "auto": "claude_md_launch_lines == 0",
     "evidence": [
      "facts§3"
     ],
     "next": "작업 폴더나 ~/.claude 에 CLAUDE.md 를 만들고 반복해서 말하던 요청을 적는다."
    },
    {
     "level": 2,
     "name": "한 범위의 지시",
     "desc": "지시 파일이 있지만 한 범위에만 있고 필요할 때만 불러오는 지시가 없다.",
     "auto": "claude_md_launch_lines > 0",
     "evidence": [
      "facts§1",
      "T1"
     ],
     "next": "특정 작업에서만 필요한 절차를 스킬이나 경로 조건 rules 로 옮기거나, 전역과 프로젝트 지시를 나눈다."
    },
    {
     "level": 3,
     "name": "범위·시점 분리",
     "desc": "지시가 전역과 프로젝트로 나뉘었거나, 경로 조건 rules·스킬·하위 폴더 CLAUDE.md 처럼 필요할 때만 들어오는 지시가 있다.",
     "auto": "claude_md_launch_lines > 0 and (claude_md_scopes >= 2 or rules_count >= 1 or own_skills >= 1 or claude_md_on_read >= 1)",
     "evidence": [
      "P2",
      "facts§2",
      "facts§3",
      "T10"
     ],
     "next": "전역·프로젝트 분리와 조건부 지시를 함께 갖추고, 시작 시 읽히는 지시의 합을 200줄 이하로 줄인다."
    },
    {
     "level": 4,
     "name": "경계를 넘는 지시",
     "desc": "범위 분리와 조건부 지시가 함께 있고 시작 시 읽히는 지시가 200줄 이하라, 메인과 일반 서브에이전트가 같은 짧은 지시를 받는다.",
     "auto": "claude_md_scopes >= 2 and (rules_count >= 1 or own_skills >= 1 or claude_md_on_read >= 1) and claude_md_launch_lines <= 200",
     "evidence": [
      "P4",
      "case1",
      "T2",
      "facts§4",
      "doc:CLAUDE.md 200줄 권고(사실표 추가 필요)"
     ],
     "next": "서브에이전트와 Explore 에이전트를 각각 띄워 지시 파일의 핵심 규칙을 알고 있는지 물어본다."
    },
    {
     "level": 5,
     "name": "도착 확인",
     "desc": "스폰된 에이전트에 지시가 실제로 도착하는지, Explore·Plan 처럼 CLAUDE.md 를 건너뛰는 경우에 무엇이 빠지는지 직접 확인했다.",
     "auto": null,
     "evidence": [
      "T2",
      "T11",
      "case1"
     ],
     "next": "현재 수준을 유지하고, 지시를 바꿀 때마다 같은 확인을 반복한다."
    }
   ]
  },
  {
   "id": "memory",
   "name": "기억과 위치",
   "summary": "세션을 넘어 남는 기억이 시작 폴더에 따라 갈라지지 않고, 기억을 받지 못하는 스폰에도 필요한 내용이 전달되는 정도.",
   "evidence": [
    "P3",
    "T4",
    "case2",
    "case1",
    "facts§1",
    "facts§3",
    "facts§4",
    "arch§9"
   ],
   "levels": [
    {
     "level": 1,
     "name": "기억 없음",
     "desc": "이 시작 폴더 기준으로 쌓인 auto memory 파일이 없다.",
     "auto": "memory_files == 0",
     "evidence": [
      "facts§3",
      "case2"
     ],
     "next": "같은 폴더에서 여러 세션을 이어 쓰며 기억이 쌓이게 하거나, 남길 내용을 기억하라고 직접 요청한다."
    },
    {
     "level": 2,
     "name": "폴더별 기억",
     "desc": "기억이 쌓이지만 git 밖이라 시작 폴더를 옮기면 다른 기억 디렉터리가 생긴다.",
     "auto": "memory_files >= 1",
     "evidence": [
      "P3",
      "T4",
      "case2"
     ],
     "next": "자주 오가는 폴더들을 하나의 git 저장소로 묶어 기억이 한 디렉터리에 모이게 한다."
    },
    {
     "level": 3,
     "name": "저장소 단위 기억",
     "desc": "기억이 git 저장소 단위로 묶여 하위 폴더와 워크트리에서 같은 기억을 보고, 색인이 읽히는 길이 안에 있다.",
     "auto": "memory_files >= 1 and in_git == 1 and memory_index_lines <= 200",
     "evidence": [
      "facts§3",
      "facts§1"
     ],
     "next": "하위 에이전트가 이어서 알아야 하는 내용이 있는 역할의 에이전트 정의에 memory 필드를 둔다."
    },
    {
     "level": 4,
     "name": "스폰용 기억",
     "desc": "auto memory 를 받지 못하는 서브에이전트를 위해 에이전트 전용 기억을 둔 정의가 있다.",
     "auto": "memory_files >= 1 and in_git == 1 and memory_index_lines <= 200 and agent_memory >= 1",
     "evidence": [
      "facts§4",
      "P4",
      "case1"
     ],
     "next": "기억 색인을 읽고 틀리거나 지난 항목을 지운다."
    },
    {
     "level": 5,
     "name": "기억 검토",
     "desc": "기억을 주기적으로 읽어 틀리거나 오래된 항목을 지우고, 하위 에이전트에 기억이 가는지 직접 확인했다.",
     "auto": null,
     "evidence": [
      "T11",
      "arch§9"
     ],
     "next": "현재 수준을 유지하고, 검토 주기를 정해 둔다."
    }
   ]
  },
  {
   "id": "tools",
   "name": "도구 범위",
   "summary": "모델이 쓸 수 있는 외부 도구가 작업과 역할에 맞게 좁혀져 있고, 연결 실패를 알아차리는 정도.",
   "evidence": [
    "P1",
    "T8",
    "T9",
    "case3",
    "case6",
    "facts§1",
    "facts§4"
   ],
   "levels": [
    {
     "level": 1,
     "name": "내장 도구만",
     "desc": "MCP 서버와 플러그인 없이 내장 도구만 쓴다.",
     "auto": "mcp_count == 0 and plugin_count == 0",
     "evidence": [
      "facts§1"
     ],
     "next": "반복해서 손으로 하던 외부 작업 하나를 MCP 서버나 플러그인으로 연결한다."
    },
    {
     "level": 2,
     "name": "전역 도구",
     "desc": "외부 도구를 붙였지만 모든 폴더에 같은 도구가 붙는다.",
     "auto": "mcp_count >= 1 or plugin_count >= 1",
     "evidence": [
      "facts§1",
      "case6"
     ],
     "next": "특정 프로젝트에서만 쓰는 도구는 그 폴더의 local 또는 project 범위로 옮기고, 이름이 겹치는 도구를 정리한다."
    },
    {
     "level": 3,
     "name": "폴더별 도구",
     "desc": "특정 시작 폴더에만 붙는 도구가 있고 같은 이름의 도구가 두 범위에 겹치지 않는다.",
     "auto": "(mcp_count >= 1 or plugin_count >= 1) and folder_mcp >= 1 and mcp_name_dupes == 0",
     "evidence": [
      "P3",
      "case3"
     ],
     "next": "역할이 분명한 에이전트 정의에 tools 또는 disallowedTools 를 적어 필요한 도구만 쓰게 한다."
    },
    {
     "level": 4,
     "name": "역할별 도구",
     "desc": "에이전트 정의에서 도구를 좁혀 역할마다 쓸 수 있는 도구가 다르다.",
     "auto": "(mcp_count >= 1 or plugin_count >= 1) and folder_mcp >= 1 and mcp_name_dupes == 0 and agents_tool_restricted >= 1",
     "evidence": [
      "facts§4",
      "case5"
     ],
     "next": "도구 연결 상태를 세션 시작 때 확인하는 방법을 정하고, 한 달 동안 쓰지 않은 도구를 뗀다."
    },
    {
     "level": 5,
     "name": "연결 관리",
     "desc": "도구 연결이 끊기면 작업 중에 알아차리고, 쓰지 않는 도구를 정리한 적이 있다.",
     "auto": null,
     "evidence": [
      "T8",
      "T11"
     ],
     "next": "현재 수준을 유지한다."
    }
   ]
  },
  {
   "id": "prevention",
   "name": "사전 차단",
   "summary": "도구가 실행되기 전에 모델의 판단과 무관하게 막는 장치(권한 규칙, 샌드박스, 행동 전 훅)가 있는 정도.",
   "evidence": [
    "P1",
    "P2",
    "T1",
    "case4",
    "facts§1",
    "facts§2",
    "facts§4",
    "arch§5"
   ],
   "levels": [
    {
     "level": 1,
     "name": "차단 없음",
     "desc": "승인 창이 없는 모드인데 모드와 무관하게 막는 장치(금지 규칙, 겨냥한 행동 전 훅, 샌드박스)가 하나도 없다. bypassPermissions 이거나, acceptEdits 에서 확인 규칙까지 없는 경우다.",
     "auto": "deny_total + pre_hooks_targeted + sandbox == 0 and (mode == \"bypassPermissions\" or (mode == \"acceptEdits\" and ask_total == 0))",
     "evidence": [
      "facts§1",
      "facts§4",
      "case4"
     ],
     "next": "위험한 명령을 deny 에 적거나 위험 도구를 겨냥한 PreToolUse 훅을 둔다. deny 규칙과 훅은 bypassPermissions 에서도 막는다."
    },
    {
     "level": 2,
     "name": "그때그때 승인",
     "desc": "미리 적어 둔 금지 규칙(bypassPermissions 가 아니면 확인 규칙 포함)이 없다. 막는 일을 승인 창이나 자동 판단 모드, 규칙 밖의 장치에 맡긴다.",
     "auto": "not (deny_total + pre_hooks_targeted + sandbox == 0 and (mode == \"bypassPermissions\" or (mode == \"acceptEdits\" and ask_total == 0)))",
     "evidence": [
      "facts§1",
      "T1"
     ],
     "next": "되돌리기 어려운 명령(삭제, 강제 푸시, 배포)을 deny 나 ask 규칙으로 적는다. bypassPermissions 에서는 ask 가 무시되므로 deny 로 적는다."
    },
    {
     "level": 3,
     "name": "적어 둔 규칙",
     "desc": "금지하거나 확인받을 동작을 권한 규칙으로 적어 두었다. bypassPermissions 에서는 확인(ask) 규칙이 무시되므로 금지(deny) 규칙만 센다.",
     "auto": "deny_total >= 1 or (mode != \"bypassPermissions\" and ask_total >= 1)",
     "evidence": [
      "P2",
      "facts§2",
      "arch§5"
     ],
     "next": "샌드박스를 켜거나 위험 도구를 겨냥한 PreToolUse 훅을 추가해 규칙과 별도의 코드 차단을 둔다."
    },
    {
     "level": 4,
     "name": "이중 차단",
     "desc": "금지 규칙과 함께 샌드박스나 특정 도구를 겨냥한 행동 전 훅이 있어, 모든 스폰에서 코드가 막는다.",
     "auto": "deny_total >= 1 and (sandbox == 1 or pre_hooks_targeted >= 1)",
     "evidence": [
      "facts§1",
      "facts§4",
      "P4"
     ],
     "next": "금지 규칙에 걸리는 명령을 일부러 실행해 실제로 막히는지 확인한다."
    },
    {
     "level": 5,
     "name": "차단 확인",
     "desc": "금지 규칙과 차단 훅이 실제로 막는 것을 직접 시험해 확인했다.",
     "auto": null,
     "evidence": [
      "T1",
      "T11"
     ],
     "next": "현재 수준을 유지하고, 규칙을 바꿀 때마다 같은 시험을 반복한다."
    }
   ]
  },
  {
   "id": "verification",
   "name": "사후 검증과 관측",
   "summary": "도구 실행 뒤와 작업 종료 시점에 결과를 검사하고 기록해, 메인과 하위 에이전트의 실패를 알아차리는 정도.",
   "evidence": [
    "P1",
    "P2",
    "T6",
    "T7",
    "T8",
    "case7",
    "facts§2",
    "facts§4"
   ],
   "levels": [
    {
     "level": 1,
     "name": "확인 없음",
     "desc": "행동 후나 종료 시점에 실행되는 훅이 없다.",
     "auto": "post_hooks == 0",
     "evidence": [
      "facts§1"
     ],
     "next": "작업이 끝날 때 알림이나 기록을 남기는 훅을 하나 둔다."
    },
    {
     "level": 2,
     "name": "기록·알림",
     "desc": "행동 후 훅이 있지만 작업 종료를 검사하는 훅은 없다.",
     "auto": "post_hooks >= 1",
     "evidence": [
      "facts§2"
     ],
     "next": "Stop 이나 SubagentStop 이벤트에 결과를 검사하는 훅(테스트 실행, 형식 검사 등)을 둔다."
    },
    {
     "level": 3,
     "name": "종료 검사",
     "desc": "메인 작업이나 하위 에이전트가 끝날 때 실행되는 훅이 있다.",
     "auto": "stop_hooks + subagentstop_hooks >= 1",
     "evidence": [
      "T7",
      "facts§2"
     ],
     "next": "PostToolUse, Stop, SubagentStop 세 지점에 각각 훅을 두어 도구·작업·하위 에이전트 단위로 확인한다."
    },
    {
     "level": 4,
     "name": "세 지점 관측",
     "desc": "도구 단위, 메인 작업 단위, 하위 에이전트 단위 세 지점 모두에 훅이 있어 스폰된 워커의 결과도 확인된다.",
     "auto": "posttooluse_hooks >= 1 and stop_hooks >= 1 and subagentstop_hooks >= 1",
     "evidence": [
      "case7",
      "T6",
      "facts§4"
     ],
     "next": "훅이 실패했을 때 기록이 남는 곳을 정하고, 검사가 실제로 문제를 잡은 사례를 확인한다."
    },
    {
     "level": 5,
     "name": "검사 확인",
     "desc": "훅 자체의 실패를 알아차리는 경로가 있고, 검사가 실제로 문제를 걸러낸 사례를 확인했다.",
     "auto": null,
     "evidence": [
      "T7",
      "T8",
      "T11"
     ],
     "next": "현재 수준을 유지한다."
    }
   ]
  },
  {
   "id": "delegation",
   "name": "위임 설계",
   "summary": "에이전트를 띄울 때 역할·모델·경계 설정이 정의 파일에 적혀 있어, 스폰 종류와 기능 설정이 바뀌어도 의도대로 전달되는 정도.",
   "evidence": [
    "P4",
    "P5",
    "T3",
    "T5",
    "T9",
    "T10",
    "case4",
    "case5",
    "case6",
    "case7",
    "facts§2",
    "facts§4"
   ],
   "levels": [
    {
     "level": 1,
     "name": "내장 위임만",
     "desc": "직접 정의한 에이전트가 없고 내장·플러그인 에이전트만 쓴다.",
     "auto": "own_agents == 0",
     "evidence": [
      "facts§4"
     ],
     "next": "자주 맡기는 역할 하나를 .claude/agents 에 정의한다."
    },
    {
     "level": 2,
     "name": "역할 정의",
     "desc": "직접 정의한 에이전트가 있지만 모두 같은 모델로 뜬다.",
     "auto": "own_agents >= 1",
     "evidence": [
      "P5",
      "T5"
     ],
     "next": "판단이 많이 필요한 역할과 단순 반복 역할에 서로 다른 모델을 지정한다."
    },
    {
     "level": 3,
     "name": "역할별 모델",
     "desc": "역할에 따라 다른 모델을 정의에 배정했다.",
     "auto": "own_agents >= 1 and own_agent_models >= 2",
     "evidence": [
      "facts§4",
      "T9"
     ],
     "next": "정의에 omitClaudeMd 등 경계 설정을 명시하고, 팀 기능과 auto·bypass 모드를 함께 켰다면 둘 중 하나를 정리한다."
    },
    {
     "level": 4,
     "name": "경계 명시",
     "desc": "스폰 경계에서 무엇이 넘어갈지 정의에 명시했고, 이름이 가려진 정의가 없으며, 팀 기능과 권한 모드가 의도치 않게 겹치지 않는다.",
     "auto": "own_agents >= 1 and own_agent_models >= 2 and boundary_explicit >= 1 and agents_shadowed == 0 and warn_teams_mode == 0",
     "evidence": [
      "P4",
      "T3",
      "case4",
      "facts§2",
      "facts§4"
     ],
     "next": "하위 에이전트에게 지시 파일의 규칙을 아는지 확인하는 질문을 넣고 결과를 대조한다."
    },
    {
     "level": 5,
     "name": "전달 확인",
     "desc": "하위 에이전트에 넘긴 지시와 설정이 실제로 도착했는지 결과에서 확인했다.",
     "auto": null,
     "evidence": [
      "T2",
      "T5",
      "T11",
      "case5"
     ],
     "next": "현재 수준을 유지하고, 스폰 규칙은 요약이 아닌 공식 문서 원문으로 확인한다."
    }
   ]
  }
 ],
 "effects": {
  "unlock4": "점검 result 가 yes 이면, blocked_by_unknown == 4 인 폴더를 4 로 바꾸고 아래쪽 중앙값을 다시 계산한다.",
  "demote": "점검 result 가 no 이고 현재 레벨이 2 이상이면 1 내린다. 같은 축에 demote 점검이 여러 개여도 최대 1 만 내린다.",
  "promote5": "demote 적용 후 레벨이 4 이고, 그 축의 promote5 점검 중 하나라도 yes 이며 no 가 하나도 없으면 5.",
  "flag": "레벨 변화 없음. 화면에 결과와 근거를 표시한다.",
  "unknown": "어떤 효과든 result 가 unknown 이면 효과 없음, 화면에 '판정 불가'.",
  "not_run": "ran == false 이거나 기록이 없으면 효과 없음, 화면에 '미점검'."
 },
 "need_level": {
  "base": 2,
  "max": 4,
  "add": {
   "instruction": "max(U1, U2)",
   "memory": "U1",
   "tools": "U3",
   "prevention": "U3",
   "verification": "max(U2, U3)",
   "delegation": "U2"
  }
 },
 "history_sources": {
  "main": "~/.claude/projects/*/*.jsonl",
  "subagent": "~/.claude/projects/*/*/subagents/*.jsonl",
  "subagent_meta": "~/.claude/projects/*/*/subagents/*.meta.json",
  "window_days": 30,
  "time_field": "timestamp",
  "folder_filter": "harness.json 의 locations[].cwd 를 경로 구분자 '/' 를 '-' 로 바꾼 디렉터리 이름으로 대응시킨다. 대응이 없으면 전체 디렉터리를 센다.",
  "privacy": "개수·도구 이름·이벤트 이름·파일 경로만 센다. 메시지 본문, 명령 전문, 도구 입력값은 기록하지 않는다."
 },
 "probes": [
  {
   "id": "instruction.refs_live",
   "axis": "instruction",
   "method": "static",
   "runner": "script",
   "effect": "demote",
   "checks": "시작 시 읽히는 CLAUDE.md 가 가리키는 파일 경로가 지금도 존재하는가.",
   "steps": [
    "locations[].context.claude_md[] 중 load == 'launch' 인 파일을 읽는다.",
    "백틱으로 감싼 토큰 가운데 '/' 를 포함하고 공백이 없는 것을 경로 후보로 뽑는다.",
    "후보를 그 CLAUDE.md 가 있는 폴더 기준, 그다음 홈 기준으로 해석해 존재 여부를 확인한다."
   ],
   "criteria": {
    "yes": "후보가 0개이거나, 후보의 80% 이상이 존재한다.",
    "no": "후보의 80% 미만이 존재한다.",
    "unknown": "CLAUDE.md 를 읽지 못했다."
   },
   "evidence": "instructions 축의 근거 T1, P4: 지시는 파일로 남아 계속 읽히므로, 낡은 지시는 모든 스폰에 낡은 채로 전달된다."
  },
  {
   "id": "instruction.arrival",
   "axis": "instruction",
   "method": "history",
   "runner": "script",
   "effect": "promote5",
   "checks": "최근 30일 서브에이전트 기록에서 지시 파일이 실제로 도착했는가.",
   "steps": [
    "subagent jsonl 에서 type == 'attachment' 이고 attachment.type == 'instructions' 인 줄을 찾는다.",
    "attachment.files[].path 가운데 CLAUDE.md 로 끝나는 경로를 모은다(본문은 읽지 않는다).",
    "같은 세션 meta.json 의 agentType 별로 집계한다."
   ],
   "criteria": {
    "yes": "일반 서브에이전트 기록 1개 이상에서 메인이 읽는 CLAUDE.md 경로가 확인된다.",
    "no": "서브에이전트 기록이 있으나 Explore·Plan 이 아닌데도 CLAUDE.md 경로가 하나도 없다.",
    "unknown": "최근 30일 서브에이전트 기록이 없다."
   },
   "evidence": "P4, T2, case1, T11. 지시 도착을 대화 기록으로 확인하는 것은 직접 시험과 같은 수준의 확인으로 본다."
  },
  {
   "id": "instruction.live_ask",
   "axis": "instruction",
   "method": "live",
   "runner": "agent",
   "effect": "promote5",
   "checks": "서브에이전트를 띄워 지시 파일의 규칙 하나를 말하게 했을 때 실제로 말하는가.",
   "run_if": "instruction.arrival 의 result 가 unknown 일 때만 실행한다.",
   "steps": [
    "general-purpose 서브에이전트를 띄워 '지금 받은 지시 파일에 적힌 규칙 하나를 그대로 인용하라. 파일을 새로 읽지 말 것.' 이라고만 묻는다.",
    "같은 질문을 Explore 에이전트에도 한다.",
    "인용문이 시작 폴더의 CLAUDE.md 에 실제로 있는 문장인지 대조한다."
   ],
   "criteria": {
    "yes": "general-purpose 가 CLAUDE.md 의 실제 문장을 인용한다.",
    "no": "general-purpose 가 인용하지 못하거나 없는 문장을 지어낸다.",
    "unknown": "서브에이전트를 띄울 수 없었다."
   },
   "evidence": "P4, T2, T11. Explore 결과는 판정에 쓰지 않고 기록만 한다(CLAUDE.md 를 건너뛰는 것이 정상)."
  },
  {
   "id": "memory.index_fit",
   "axis": "memory",
   "method": "static",
   "runner": "script",
   "effect": "demote",
   "checks": "기억 색인(MEMORY.md)이 읽히는 길이 안에 있고 색인과 기억 파일이 서로 맞는가.",
   "steps": [
    "locations[].context.auto_memory.dir 의 MEMORY.md 를 연다.",
    "줄 수와 크기를 센다.",
    "색인의 링크 대상 파일이 있는지, 디렉터리의 .md 파일이 모두 색인에 있는지 비교한다."
   ],
   "criteria": {
    "yes": "모든 기억 디렉터리에서 200줄·25KB 이하이고 빠진 링크·빠진 색인이 없다.",
    "no": "한 곳이라도 길이 초과, 깨진 링크, 색인에 없는 파일이 있다.",
    "unknown": "기억 디렉터리가 하나도 없다."
   },
   "evidence": "facts§1 (앞 200줄 또는 25KB 만 읽힘), case2."
  },
  {
   "id": "memory.reviewed",
   "axis": "memory",
   "method": "history",
   "runner": "script",
   "effect": "promote5",
   "checks": "최근 30일 동안 기존 기억 파일을 고치거나 지운 기록이 있는가.",
   "steps": [
    "main jsonl 에서 tool_use 이름이 Edit 이고 input.file_path 가 '/memory/' 를 포함하는 호출 수를 센다.",
    "Bash tool_use 의 명령에서 '/memory/' 경로와 rm 이 함께 있는 호출 수를 센다(명령은 기록하지 않는다)."
   ],
   "criteria": {
    "yes": "둘 중 하나가 1 이상이다.",
    "no": "0 이다(새 기억을 쓰기만 했다).",
    "unknown": "기록이 없다."
   },
   "evidence": "T11, arch§9. 기억은 쌓기만 하면 틀린 항목이 계속 주입된다."
  },
  {
   "id": "memory.subagent_receives",
   "axis": "memory",
   "method": "history",
   "runner": "script",
   "effect": "flag",
   "checks": "서브에이전트 기록에 auto memory 색인이 도착했는가(문서와 관찰의 충돌 확인용).",
   "steps": [
    "subagent jsonl 의 instructions attachment 에서 files[].type == 'AutoMem' 인 항목이 있는지 센다."
   ],
   "criteria": {
    "yes": "1 이상(문서와 다르게 도착함).",
    "no": "0.",
    "unknown": "서브에이전트 기록이 없다."
   },
   "evidence": "arch§9 C1. 레벨에는 영향을 주지 않고 화면의 충돌 표시에 쓴다."
  },
  {
   "id": "tools.restricted",
   "axis": "tools",
   "method": "static",
   "runner": "script",
   "effect": "unlock4",
   "checks": "사용자·프로젝트 에이전트 정의 중 tools 또는 disallowedTools 로 도구를 좁힌 것이 있는가.",
   "steps": [
    "locations[].agents[] 중 scope 가 user·project 인 정의 파일의 머리말을 읽는다.",
    "tools 또는 disallowedTools 키의 유무만 확인한다."
   ],
   "criteria": {
    "yes": "1개 이상 있다.",
    "no": "없다.",
    "unknown": "정의 파일을 읽지 못했다."
   },
   "evidence": "facts§4, case5. 추출기에 agents[].tools_restricted 가 생기면 이 점검은 자동 조건으로 흡수된다."
  },
  {
   "id": "tools.unused",
   "axis": "tools",
   "method": "history",
   "runner": "script",
   "effect": "demote",
   "checks": "붙여 둔 MCP 서버가 모두 최근 30일 안에 한 번 이상 쓰였는가.",
   "steps": [
    "harness.json 의 MCP 이름 목록을 만든다.",
    "main·subagent jsonl 의 tool_use 이름이 'mcp__<서버이름>__' 로 시작하는 호출을 서버별로 센다(플러그인 MCP 는 'mcp__plugin_' 접두 포함)."
   ],
   "criteria": {
    "yes": "모든 서버가 1회 이상.",
    "no": "0회인 서버가 하나 이상(서버 이름과 0 을 기록).",
    "unknown": "기록이 없다."
   },
   "evidence": "P1, T8. 쓰지 않는 도구도 모든 서브에이전트에 따라간다(facts§4 MCP carry)."
  },
  {
   "id": "tools.connected",
   "axis": "tools",
   "method": "live",
   "runner": "agent",
   "effect": "promote5",
   "checks": "설정된 MCP 서버가 지금 모두 연결되는가.",
   "steps": [
    "읽기 전용 명령 'claude mcp list' 를 실행한다.",
    "harness.json 의 MCP 목록과 대조해 서버별 연결 상태만 기록한다(주소·토큰은 기록하지 않는다)."
   ],
   "criteria": {
    "yes": "모든 서버가 연결됨.",
    "no": "하나 이상 실패(서버 이름 기록).",
    "unknown": "명령을 실행할 수 없었다."
   },
   "evidence": "T8, case3. 설치 목록과 실행 시점 도구는 다르다."
  },
  {
   "id": "prevention.mode_drift",
   "axis": "prevention",
   "method": "history",
   "runner": "script",
   "effect": "demote",
   "checks": "실제 세션이 설정 파일의 권한 모드보다 느슨한 모드(bypassPermissions) 없이 돌았는가.",
   "steps": [
    "main jsonl 의 type == 'permission-mode' 줄과 subagent meta.json 의 permissionMode 값을 센다(세션 수 기준).",
    "harness.json 의 enforcement.mode.value 와 비교한다."
   ],
   "criteria": {
    "yes": "설정보다 느슨한 모드(bypassPermissions)로 돈 세션이 0.",
    "no": "1개 이상(세션 수만 기록).",
    "unknown": "모드 기록이 없다."
   },
   "evidence": "P2(CLI 인자가 user 설정을 덮어씀), case4, T3. 설정 파일만 보면 auto 인데 실제로는 bypass 로 도는 경우를 잡는다."
  },
  {
   "id": "prevention.deny_test",
   "axis": "prevention",
   "method": "live",
   "runner": "agent",
   "effect": "promote5",
   "checks": "deny 규칙 각각이 실제로 막는가.",
   "steps": [
    "mktemp -d 로 새 임시 폴더를 만들고 그 안에 임시 파일 하나를 만든다.",
    "deny 규칙마다 그 규칙에 걸리는 형태의 명령을 임시 파일만 대상으로 만들어 시도한다(예: Bash(rm:*) 이면 'rm <임시폴더>/probe.txt').",
    "도구 결과가 권한 거부인지 기록한다. 끝나면 임시 폴더를 지운다."
   ],
   "criteria": {
    "yes": "시험한 deny 규칙이 모두 거부되었다.",
    "no": "하나라도 실행되었다(규칙 문자열과 결과만 기록).",
    "unknown": "deny 규칙이 없거나 시험 가능한 형태가 아니다(ran: false)."
   },
   "evidence": "T1, T11, facts§1(권한 평가 순서)."
  },
  {
   "id": "prevention.block_seen",
   "axis": "prevention",
   "method": "history",
   "runner": "script",
   "effect": "promote5",
   "checks": "최근 30일에 행동 전 훅이나 권한 규칙이 실제로 도구 호출을 막은 기록이 있는가.",
   "steps": [
    "attachment.type == 'hook_blocking_error' 이고 hookEvent 가 PreToolUse·PermissionRequest 인 줄을 센다.",
    "tool_result 앞부분이 'PreToolUse:<도구> hook error:' 형태인 수를 센다(패턴 일치 여부만, 본문은 옮기지 않는다). PreToolUse 차단은 attachment 가 아니라 이 형태로 남는 버전이 있다.",
    "권한 거부로 끝난 tool_result 수를 센다(본문 없이 개수만)."
   ],
   "criteria": {
    "yes": "합이 1 이상.",
    "no": "0.",
    "unknown": "기록이 없다."
   },
   "evidence": "T1. deny_test 를 실행하지 못한 경우의 대체 근거. tool_result 형태는 Claude Code 2.1.284 기록 제보(GitHub 이슈 #3)."
  },
  {
   "id": "prevention.bypass_mode",
   "axis": "prevention",
   "method": "static",
   "runner": "script",
   "effect": "flag",
   "checks": "시작 폴더의 기본 권한 모드가 bypassPermissions 가 아닌가.",
   "steps": [
    "시작 폴더마다 harness.json 의 enforcement.mode.value 를 읽는다.",
    "bypassPermissions 인 폴더 수와, 그 폴더의 확인(ask) 규칙 수를 센다."
   ],
   "criteria": {
    "yes": "bypassPermissions 인 시작 폴더가 0.",
    "no": "1개 이상. 승인 창, 확인(ask) 규칙, 허용(allow) 규칙, 보호 경로 확인이 꺼진다. 금지(deny) 규칙과 행동 전 훅은 그대로 막으므로 레벨은 내리지 않는다.",
    "unknown": "모드 값을 읽지 못했다."
   },
   "evidence": "facts§1, case4. 공식 문서 permission-modes: 'Deny rules block in every mode, including bypassPermissions. Allow rules have no effect in bypassPermissions.' (2026-10-06 확인, GitHub 이슈 #2). 모드가 위험한 것과 차단 장치가 없는 것은 다른 문제라 레벨이 아니라 표시로 둔다."
  },
  {
   "id": "verification.real_check",
   "axis": "verification",
   "method": "static",
   "runner": "agent",
   "effect": "demote",
   "checks": "행동 후 훅 가운데 상태 표시·알림이 아니라 무언가를 검사하는 훅이 있는가.",
   "steps": [
    "POST 이벤트 훅의 command 가 가리키는 스크립트를 읽는다(비밀값이 보이면 기록하지 않는다).",
    "조건에 따라 0 이 아닌 종료 코드나 decision: block 을 낼 수 있는 분기가 있는지 본다."
   ],
   "criteria": {
    "yes": "그런 분기를 가진 POST 훅이 1개 이상.",
    "no": "모든 POST 훅이 항상 0 으로 끝나거나 알림만 보낸다.",
    "unknown": "스크립트를 읽지 못했다."
   },
   "evidence": "T7, case7. 자동 조건은 훅의 존재만 보므로 내용을 확인한다."
  },
  {
   "id": "verification.caught",
   "axis": "verification",
   "method": "history",
   "runner": "script",
   "effect": "promote5",
   "checks": "최근 30일에 행동 후·종료 시점 검사가 실제로 무언가를 걸러낸 기록이 있는가.",
   "steps": [
    "attachment.type == 'hook_blocking_error' 이고 hookEvent 가 PostToolUse·Stop·SubagentStop 인 줄을 센다.",
    "attachment.type == 'hook_non_blocking_error' 수도 함께 센다(훅 자체의 실패)."
   ],
   "criteria": {
    "yes": "POST 이벤트의 blocking_error 가 1 이상.",
    "no": "0.",
    "unknown": "기록이 없다."
   },
   "evidence": "T7, T8. non_blocking_error 수는 훅 실패가 기록에 남는다는 근거로 함께 저장한다."
  },
  {
   "id": "verification.in_subagents",
   "axis": "verification",
   "method": "history",
   "runner": "script",
   "effect": "flag",
   "checks": "서브에이전트 기록에서 훅이 실행되었는가.",
   "steps": [
    "subagent jsonl 의 hook_success·hook_*_error attachment 를 hookEvent 별로 센다."
   ],
   "criteria": {
    "yes": "PreToolUse 또는 PostToolUse 가 1 이상.",
    "no": "0.",
    "unknown": "서브에이전트 기록이 없다."
   },
   "evidence": "facts§4(훅은 서브에이전트 도구 호출에도 실행), case7."
  },
  {
   "id": "delegation.used",
   "axis": "delegation",
   "method": "history",
   "runner": "script",
   "effect": "demote",
   "checks": "직접 정의한 에이전트가 최근 30일 안에 실제로 띄워졌는가.",
   "steps": [
    "main jsonl 의 tool_use 이름 Agent(옛 이름 Task) 호출에서 input.subagent_type 만 읽어 이름별로 센다.",
    "subagent meta.json 의 agentType 도 함께 센다.",
    "harness.json 의 사용자·프로젝트 에이전트 이름과 대조한다."
   ],
   "criteria": {
    "yes": "정의한 에이전트 중 1개 이상이 1회 이상 띄워졌다.",
    "no": "하나도 띄워지지 않았다.",
    "unknown": "기록이 없다."
   },
   "evidence": "P5, T5. 정의가 있어도 쓰지 않으면 위임 설계가 작동하지 않는다."
  },
  {
   "id": "delegation.config_match",
   "axis": "delegation",
   "method": "history",
   "runner": "script",
   "effect": "promote5",
   "checks": "띄운 에이전트의 실제 모델·권한 모드가 정의 파일과 의도에 맞게 적용되었는가.",
   "steps": [
    "subagent meta.json 의 agentType, model, permissionMode, taskKind 를 읽는다.",
    "agentType 이 사용자·프로젝트 정의와 같으면 정의의 model 과 비교한다.",
    "taskKind 가 팀원인데 permissionMode 가 bypassPermissions 인 건수를 따로 센다."
   ],
   "criteria": {
    "yes": "정의한 에이전트 기록이 1개 이상 있고, 모델이 모두 정의와 맞으며, 의도치 않은 bypass 전파가 0.",
    "no": "모델 불일치 또는 bypass 전파가 1건 이상.",
    "unknown": "정의한 에이전트의 기록이 없다."
   },
   "evidence": "P4, T3, T9, case4, facts§4(모델 결정 순서, bypass 전원 전파)."
  }
 ],
 "usage_probes": [
  {
   "id": "U1",
   "name": "공유 범위",
   "method": "static",
   "runner": "script",
   "checks": "이 설정을 조직이나 여러 사람이 함께 쓰는가.",
   "steps": [
    "managed 설정 파일(macOS: /Library/Application Support/ClaudeCode/managed-settings.json) 존재를 확인한다.",
    "각 location 의 .claude/settings.json·.claude/agents·CLAUDE.md 가 git ls-files 로 추적되는지 확인한다."
   ],
   "value": {
    "2": "managed 설정이 있다.",
    "1": "managed 는 없고 프로젝트 설정·정의·CLAUDE.md 중 하나가 git 에 커밋되어 있다.",
    "0": "둘 다 아니다."
   },
   "ask_fallback": "git 밖에서 설정을 복사해 나눠 쓰는 경우는 데이터로 알 수 없으므로, 값이 0 일 때만 '이 설정을 다른 사람과 복사해 나눠 쓰는가'를 물을 수 있다(선택)."
  },
  {
   "id": "U2",
   "name": "스폰 빈도",
   "method": "history",
   "runner": "script",
   "checks": "최근 30일에 하위 에이전트를 얼마나 띄웠는가.",
   "steps": [
    "main jsonl 의 Agent·Task tool_use 수와 subagents/*.meta.json 수를 센다(큰 값을 쓴다)."
   ],
   "value": {
    "0": "0~9회",
    "1": "10~89회",
    "2": "90회 이상(하루 평균 3회 이상)"
   }
  },
  {
   "id": "U3",
   "name": "되돌리기 어려운 작업",
   "method": "history",
   "runner": "script",
   "checks": "최근 30일에 되돌리기 어려운 명령을 얼마나 실행했는가.",
   "steps": [
    "main·subagent jsonl 의 Bash tool_use 에서 명령 문자열을 패턴과 대조만 하고 기록하지 않는다.",
    "패턴: 'git push', 'rm -rf', 'deploy', 'terraform apply', 'kubectl apply', 'kubectl delete', 'npm publish', 'gh release create', 'DROP TABLE'. 패턴별 개수만 저장한다."
   ],
   "value": {
    "0": "0회",
    "1": "1~9회",
    "2": "10회 이상"
   }
  }
 ],
 "probe_record": {
  "id": "점검 id",
  "method": "static|live|history|ask",
  "ran": "true|false",
  "result": "yes|no|unknown",
  "evidence": "실행한 명령이나 읽은 파일, 관찰 한두 문장. 본문·비밀값 금지",
  "at": "ISO 8601 시각",
  "counts": "(선택) history 점검의 개수 묶음"
 },
 "probe_storage": {
  "script": "harness-map.py 가 runner == script 점검을 실행해 harness.json 의 'probes' 배열에 쓴다.",
  "agent": "에이전트가 runner == agent 점검을 실행해 harness-map-output/probes.json 의 배열에 쓴다.",
  "merge": "build.py 가 두 배열을 id 로 합친다. 같은 id 가 둘 다 있으면 at 이 늦은 쪽을 쓴다. usage 점검 결과는 {\"id\":\"U2\",\"value\":1,...} 형식."
 },
 "live_safety": [
  "시험은 mktemp -d 로 새로 만든 임시 폴더 안의 임시 파일만 대상으로 한다. 끝나면 그 폴더를 지운다.",
  "금지 규칙 시험은 그 규칙에 걸리는 형태의 명령을 임시 파일에만 시도한다. 규칙이 없어 실행되어도 피해가 없어야 한다.",
  "외부 발송, 푸시, 배포, 결제, 원격 API 쓰기 시험은 하지 않는다.",
  "설정 파일(settings.json, CLAUDE.md, 에이전트 정의, .mcp.json)을 고치지 않는다.",
  "비밀값(토큰, 키, 주소의 인증 정보)을 evidence 에 기록하지 않는다.",
  "서브에이전트 시험은 '지시 파일의 규칙 하나를 말해 보라' 같은 무해한 질문만 한다."
 ]
}''')

_CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
        ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def _ev(node, m):
    """채점식 평가. 지표·정수·문자열·비교·+·and/or/not 만 허용. None 은 '판정 불가'(세 값 논리)."""
    if isinstance(node, ast.Expression):
        return _ev(node.body, m)
    if isinstance(node, ast.BoolOp):
        vs = [_ev(v, m) for v in node.values]
        if isinstance(node.op, ast.And):
            return False if False in vs else (None if None in vs else True)
        return True if True in vs else (None if None in vs else False)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        v = _ev(node.operand, m)
        return None if v is None else not v
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _ev(node.left, m), _ev(node.right, m)
        return None if a is None or b is None else a + b
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP:
        a, b = _ev(node.left, m), _ev(node.comparators[0], m)
        return None if a is None or b is None else _CMP[type(node.ops[0])](a, b)
    if isinstance(node, ast.Name):
        if node.id not in m:
            raise ValueError(f"채점표에 없는 지표: {node.id}")
        return m[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)):
        return node.value
    raise ValueError("채점식에 허용되지 않은 문법이 있습니다")


def _names(expr: str) -> list[str]:
    return sorted({n.id for n in ast.walk(ast.parse(expr, mode="eval")) if isinstance(n, ast.Name)})


def metrics_of(L: dict) -> dict:
    """한 시작 폴더의 조립 결과에서 채점 지표를 계산한다. 데이터에 없는 필드는 None."""
    hs = SCORECARD["hook_sets"]
    ctx, enf = L["context"], L["enforcement"]
    md = ctx["claude_md"]
    launch = [c for c in md if c["load"] == "launch"]
    own_ag = [a for a in L["agents"] if a["scope"] in ("user", "project")]
    hooks = enf["hooks"]
    names = [x["name"] for x in L["tools"]["mcp"]]
    restricted = [a.get("tools_restricted") for a in L["agents"] if "tools_restricted" in a]
    return {
        "claude_md_launch_lines": sum(c["lines"] for c in launch),
        "claude_md_scopes": len({c["scope"] for c in launch}),
        "rules_count": len(ctx["rules"]),
        "own_skills": sum(1 for k in ctx["skills"] if k["scope"] in ("user", "project")),
        "memory_files": ctx["auto_memory"]["files"],
        "memory_index_lines": ctx["auto_memory"]["index_lines"],
        "in_git": 1 if L.get("git_root") else 0,
        "agent_memory": sum(1 for a in own_ag if a.get("has_memory")),
        "mcp_count": len(names),
        "plugin_count": len(L["plugins"]),
        "folder_mcp": sum(1 for x in L["tools"]["mcp"] if x["scope"] in ("local", "project")),
        "mcp_name_dupes": len(names) - len(set(names)),
        "agents_tool_restricted": sum(1 for r in restricted if r) if restricted else None,
        "mode": enf["mode"]["value"],
        "deny_total": sum(p["deny"] for p in enf["perm"]),
        "ask_total": sum(p["ask"] for p in enf["perm"]),
        "sandbox": 1 if enf["sandbox"] else 0,
        "pre_hooks_targeted": sum(1 for h in hooks if h["event"] == "PreToolUse" and h["matcher"] not in ("*", "")),
        "post_hooks": sum(1 for h in hooks if h["event"] in hs["POST"]),
        "posttooluse_hooks": sum(1 for h in hooks if h["event"] == "PostToolUse"),
        "stop_hooks": sum(1 for h in hooks if h["event"] == "Stop"),
        "subagentstop_hooks": sum(1 for h in hooks if h["event"] == "SubagentStop"),
        "own_agents": len(own_ag),
        "own_agent_models": len({a["model"] for a in own_ag if a.get("model") not in (None, "inherit")}),
        "boundary_explicit": sum(1 for a in own_ag if a.get("omitClaudeMd") is not None
                                 or a.get("permissionMode") is not None or a.get("skills") is not None),
        "agents_shadowed": sum(1 for a in L["agents"] if a.get("shadowed_by")),
        "warn_teams_mode": 1 if any(w["id"] == "teams-inherit-mode" for w in L["warnings"]) else 0,
        "claude_md_on_read": sum(1 for c in md if c["load"] == "on-read"),
    }


def _lower_median(xs: list[int]) -> int:
    s = sorted(xs)
    return s[(len(s) - 1) // 2]


def score(locations: list[dict]) -> dict:
    """축마다 폴더별 자동 레벨을 구하고 아래쪽 중앙값으로 합친다. 레벨 5는 자동으로 주지 않는다."""
    per_loc = [(L["cwd"], metrics_of(L)) for L in locations]
    axes = []
    for ax in SCORECARD["axes"]:
        used = sorted({n for lv in ax["levels"] if lv["auto"] for n in _names(lv["auto"])})
        by_loc = []
        for cwd, m in per_loc:
            level, blocked, checks = 1, None, []
            for lv in ax["levels"][1:SCORECARD["auto_cap"]]:   # 레벨 2, 3, 4
                r = _ev(ast.parse(lv["auto"], mode="eval"), m)
                checks.append({"level": lv["level"], "expr": lv["auto"], "result": r})
                if r is True:
                    level = lv["level"]
                    continue
                if r is None:
                    blocked = lv["level"]
                break
            by_loc.append({"cwd": cwd, "level": level, "blocked_by_unknown": blocked,
                           "metrics": {k: m[k] for k in used}, "checks": checks})
        lv_list = [b["level"] for b in by_loc]
        axes.append({"id": ax["id"], "auto_level": _lower_median(lv_list) if lv_list else 1,
                     "range": [min(lv_list), max(lv_list)] if lv_list else [1, 1], "by_location": by_loc})
    return {"spec_version": SCORECARD["version"], "aggregate": SCORECARD["aggregate"], "axes": axes}


# ---------------------------------------------------------------- 점검(probes)
# 6장 probes·usage_probes 중 runner == "script" 인 static·history 점검을 수행한다.
# history 점검은 개수·도구 이름·이벤트 이름·파일 경로만 센다. 대화 본문, 명령 전문, 도구 입력값은 기록하지 않는다.
#
# 대화 기록(jsonl) 구조: 2026-10-02, Claude Code 2.1.287 기록을 직접 열어 확인한 필드 경로.
#   공통      : 줄마다 JSON 하나. 시각은 .timestamp (UTC ISO, 'Z' 끝). permission-mode 줄에는 timestamp 가 없다.
#   권한 모드 : .type == "permission-mode" 이고 값은 .permissionMode (예: "bypassPermissions"). 세션마다 여러 줄 반복된다.
#   도구 호출 : .type == "assistant" 의 .message.content[] 중 .type == "tool_use" → .name, .input
#               Agent(옛 Task) 는 .input.subagent_type, Edit 는 .input.file_path, Bash 는 .input.command
#   도구 결과 : .type == "user" 의 .message.content[] 중 .type == "tool_result" → .content (문자열 또는 [{type:text,text}])
#               권한 규칙·자동 판단에 의한 거부는 결과 문장이 "Permission to use ..." 로 시작하고 "denied" 를 포함한다.
#               사용자가 거절한 경우는 .toolDenialKind == "user-rejected" 로 따로 남는다(차단 집계에 넣지 않는다).
#               PreToolUse 훅의 차단은 attachment 가 아니라 결과 문장 "PreToolUse:<도구> hook error: [<명령>]: <stderr>" 로
#               남는 버전이 있다(2.1.284 기록 제보, GitHub 이슈 #3). 이 작성자 기록에는 PreToolUse 차단이 없어 직접 확인하지 못했다.
#   훅 기록   : .type == "attachment" 의 .attachment.type ∈ {hook_success, hook_blocking_error, hook_non_blocking_error,
#               hook_additional_context}, 이벤트는 .attachment.hookEvent (예: "PostToolUse").
#   지시 도착 : .attachment.type == "instructions" 의 .attachment.files[] → .path, .type ("User", "Project", "AutoMem" 등).
#               .content(본문)는 읽지 않는다.
#   서브에이전트: <세션id>/subagents/agent-*.jsonl (구조는 위와 같음) 과 같은 이름의 .meta.json
#               → .agentType, .customAgentType(팀원으로 뜬 정의 에이전트의 정의 이름), .model, .permissionMode,
#               .taskKind ("in_process_teammate" 이면 팀원). meta 에는 시각이 없어
#               짝이 되는 jsonl 의 timestamp 로 기간을 판정한다.
DANGER_PATTERNS = ["git push", "rm -rf", "deploy", "terraform apply", "kubectl apply", "kubectl delete",
                   "npm publish", "gh release create", "DROP TABLE"]
_PRE_BLOCK = re.compile(r"PreToolUse:[A-Za-z0-9_.-]+ hook (?:blocking )?error")
_RM_START = re.compile(r"(?:^|&&|;|\||\n)\s*(?:sudo\s+)?rm\s")
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
                        r"|akc_[A-Za-z0-9_-]{10,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|ntn_[A-Za-z0-9]{20,})")


def _danger_start(cmd: str, p: str) -> bool:
    """위험 명령 패턴을 명령의 시작 위치(문자열 처음, &&, ;, |, 줄바꿈 뒤)에서만 찾는다.
    'deploy' 는 첫 단어가 deploy 를 포함하는 경우(./deploy.sh 등)도 센다. 다른 패턴은 정확히 그 말로 시작해야 한다."""
    if p == "deploy":
        return bool(re.search(r"(?:^|&&|;|\||\n)\s*(?:sudo\s+)?\S*deploy", cmd))
    return bool(re.search(r"(?:^|&&|;|\||\n)\s*(?:sudo\s+)?" + re.escape(p) + r"(?:\s|$)", cmd, re.I if p == "DROP TABLE" else 0))


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _probe_def(pid: str) -> dict:
    return next(p for p in SCORECARD["probes"] if p["id"] == pid)


def _rec(pid: str, ran: bool, result: str, evidence: str, counts: dict | None = None, by: str = "script") -> dict:
    d = _probe_def(pid)
    r = {"id": pid, "axis": d["axis"], "method": d["method"], "by": by, "effect": d["effect"],
         "ran": ran, "result": result, "evidence": evidence, "at": _now_iso()}
    if counts is not None:
        r["counts"] = counts
    return r


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def history_dirs(locations: list[dict]) -> tuple[list[Path], str]:
    """기록 디렉터리: 시작 폴더 경로의 구분자를 '-' 로 바꾼 이름. 하나도 없으면 전체 디렉터리."""
    base = CLAUDE / "projects"
    dirs = []
    for L in locations:
        d = base / project_slug(Path(os.path.expanduser(L["cwd"].replace("~", str(HOME), 1))))
        if d.is_dir() and d not in dirs:
            dirs.append(d)
    if dirs:
        return dirs, f"시작 폴더 {len(locations)}곳 중 기록 디렉터리가 있는 {len(dirs)}곳"
    return ([d for d in base.iterdir() if d.is_dir()] if base.is_dir() else []), "시작 폴더와 대응하는 기록이 없어 전체 기록 디렉터리"


def scan_history(dirs: list[Path], days: int, exclude: set[str]) -> dict:
    """기록에서 개수만 센다. 본문·명령·입력값은 패턴 대조에만 쓰고 저장하지 않는다."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    H = {"main_sessions": 0, "bypass_sessions": 0, "mode_sessions": 0, "excluded": 0,
         "tool_names": Counter(), "agent_calls": 0, "agent_types": Counter(),
         "memory_edits": 0, "memory_rm": 0, "danger": Counter(),
         "block_by_event": Counter(), "nonblock": 0, "perm_denied": 0, "pre_result_blocks": 0,
         "sub_records": 0, "sub_hooks": Counter(), "sub_instr_records": 0,
         "sub_instr_paths": Counter(), "sub_instr_by_type": Counter(), "sub_automem": 0,
         "sub_nonexplore_records": 0, "sub_nonexplore_with_md": 0, "metas": []}

    def scan_file(f: Path, sub: bool) -> dict | None:
        loc = {"in": False, "modes": set(), "instr": set(), "automem": False}
        try:
            fh = f.open(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                t = o.get("type")
                ts = o.get("timestamp")
                if t == "permission-mode":            # 시각 없음: 세션 단위로만 쓴다
                    loc["modes"].add(o.get("permissionMode"))
                    continue
                if not ts or ts[:19] < cutoff:
                    continue
                loc["in"] = True
                if t == "assistant":
                    for c in (o.get("message") or {}).get("content") or []:
                        if not isinstance(c, dict) or c.get("type") != "tool_use":
                            continue
                        name, inp = c.get("name", ""), c.get("input") or {}
                        H["tool_names"][name] += 1
                        if name in ("Agent", "Task") and not sub:
                            H["agent_calls"] += 1
                            H["agent_types"][str(inp.get("subagent_type") or "general-purpose")] += 1
                        if name == "Edit" and "/memory/" in str(inp.get("file_path", "")) and not sub:
                            H["memory_edits"] += 1
                        if name == "Bash":
                            cmd = str(inp.get("command", ""))
                            if not sub and "/memory/" in cmd and _RM_START.search(cmd):
                                H["memory_rm"] += 1
                            for p in DANGER_PATTERNS:
                                if _danger_start(cmd, p):
                                    H["danger"][p] += 1
                elif t == "user":
                    for c in (o.get("message") or {}).get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            txt = _text_of(c.get("content"))[:300]
                            if txt.startswith("Permission to use") and "denied" in txt:
                                H["perm_denied"] += 1
                            elif _PRE_BLOCK.match(txt.lstrip()):
                                H["pre_result_blocks"] += 1
                elif t == "attachment":
                    a = o.get("attachment") or {}
                    at, ev = a.get("type", ""), a.get("hookEvent")
                    if at == "hook_blocking_error":
                        H["block_by_event"][ev] += 1
                    elif at == "hook_non_blocking_error":
                        H["nonblock"] += 1
                    if sub and (at == "hook_success" or (at.startswith("hook_") and at.endswith("_error"))):
                        H["sub_hooks"][ev] += 1
                    if sub and at == "instructions":
                        for x in a.get("files") or []:
                            if str(x.get("path", "")).endswith("CLAUDE.md"):
                                loc["instr"].add(str(x["path"]))
                            if x.get("type") == "AutoMem":
                                loc["automem"] = True
        return loc

    for d in dirs:
        for f in sorted(d.glob("*.jsonl")):
            if f.stem in exclude:
                H["excluded"] += 1
                continue
            r = scan_file(f, False)
            if not r or not r["in"]:
                continue
            H["main_sessions"] += 1
            if r["modes"]:
                H["mode_sessions"] += 1
            if "bypassPermissions" in r["modes"]:
                H["bypass_sessions"] += 1
        for f in sorted(d.glob("*/subagents/*.jsonl")):
            if f.parent.parent.name in exclude:
                continue
            r = scan_file(f, True)
            if not r or not r["in"]:
                continue
            H["sub_records"] += 1
            meta = read_json(f.parent / (f.stem + ".meta.json"))
            atype = str(meta.get("agentType") or "unknown")
            # 팀원으로 뜬 정의 에이전트는 agentType 에 팀원 이름이, customAgentType 에 정의 이름이 남는다.
            H["metas"].append({"agentType": meta.get("customAgentType") or meta.get("agentType"),
                               **{k: meta.get(k) for k in ("model", "permissionMode", "taskKind")}})
            if r["instr"]:
                H["sub_instr_records"] += 1
                H["sub_instr_by_type"][atype] += 1
                for pth in r["instr"]:
                    H["sub_instr_paths"][pth] += 1
            if r["automem"]:
                H["sub_automem"] += 1
            if atype not in ("Explore", "Plan"):
                H["sub_nonexplore_records"] += 1
                if r["instr"]:
                    H["sub_nonexplore_with_md"] += 1
    return H


def _launch_md(locations: list[dict]) -> list[str]:
    seen = []
    for L in locations:
        for c in L["context"]["claude_md"]:
            if c["load"] == "launch" and c["path"] not in seen:
                seen.append(c["path"])
    return seen


def _own_agents(locations: list[dict]) -> dict:
    out = {}
    for L in locations:
        for a in L["agents"]:
            if a["scope"] in ("user", "project") and not a.get("shadowed_by"):
                out.setdefault(a["name"], a)
    return out


def _real(p: str) -> Path:
    return Path(p.replace("~", str(HOME), 1)) if p.startswith("~") else Path(p)


# ---- static
def probe_refs_live(locations: list[dict]) -> dict:
    files, cands, ok, missing, unreadable = _launch_md(locations), 0, 0, [], 0
    for fp in files:
        p = _real(fp)
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            unreadable += 1
            continue
        for tok in sorted(set(re.findall(r"`([^`\s]*/[^`\s]*)`", text))):
            # URL, 자리표시자(<이름>, YYYY, {a,b}, V1~V6 처럼 중간에 ~ 가 있는 범위 표기)는 경로 후보에서 뺀다.
            if "://" in tok or "<" in tok or "{" in tok or "YYYY" in tok or "~" in tok[1:]:
                continue
            cands += 1
            raw = tok.rstrip(".,")
            # 그 CLAUDE.md 의 폴더, 홈, 그리고 시작 폴더(전역 지시가 프로젝트 상대 경로를 가리키는 경우) 순서로 해석한다.
            bases = [_real(raw)] if raw.startswith(("~", "/")) else [p.parent / raw, HOME / raw] + [_real(L["cwd"]) / raw for L in locations]
            hit = any((glob.glob(str(b)) if any(ch in raw for ch in "*?[") else b.exists()) for b in bases)
            ok += hit
            if not hit and len(missing) < 5:
                missing.append(raw)
    if not files or unreadable == len(files):
        return _rec("instruction.refs_live", True, "unknown", "시작 시 읽히는 CLAUDE.md 를 읽지 못했다.")
    rate = ok / cands if cands else 1.0
    res = "yes" if cands == 0 or rate >= 0.8 else "no"
    ev = (f"시작 시 읽히는 CLAUDE.md {len(files)}개에서 백틱으로 감싼 경로 후보 {cands}개를 찾았고 그중 {ok}개가 존재한다."
          if cands else f"시작 시 읽히는 CLAUDE.md {len(files)}개에 경로 후보가 없다.")
    return _rec("instruction.refs_live", True, res, ev, {"files": len(files), "candidates": cands, "exist": ok, "missing_sample": missing})


def probe_index_fit(locations: list[dict]) -> dict:
    dirs = []
    for L in locations:
        d = _real(L["context"]["auto_memory"]["dir"])
        if d.is_dir() and d not in dirs:
            dirs.append(d)
    if not dirs:
        return _rec("memory.index_fit", True, "unknown", "기억 디렉터리가 하나도 없다.")
    bad, rows = 0, []
    for d in dirs:
        idx = d / "MEMORY.md"
        try:
            text = idx.read_text(encoding="utf-8", errors="ignore")
            size = idx.stat().st_size
        except OSError:
            text, size = "", 0
        n = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        links = {m for m in re.findall(r"\]\(([^)#\s]+\.md)\)", text)}
        broken = sum(1 for m in links if not (d / m).exists())
        files = {f.name for f in d.glob("*.md") if f.name != "MEMORY.md"}
        unindexed = len(files - {Path(m).name for m in links})
        fail = n > 200 or size > 25 * 1024 or broken or unindexed or not idx.exists()
        bad += bool(fail)
        rows.append({"lines": n, "kb": round(size / 1024, 1), "broken_links": broken, "unindexed": unindexed, "index_exists": idx.exists()})
    ev = (f"기억 디렉터리 {len(dirs)}곳을 확인했고 {bad}곳에서 길이 초과, 깨진 링크, 색인에 없는 파일 중 하나가 발견되었다."
          if bad else f"기억 디렉터리 {len(dirs)}곳 모두 색인이 200줄·25KB 이하이고 색인과 파일이 서로 맞는다.")
    return _rec("memory.index_fit", True, "no" if bad else "yes", ev, {"dirs": rows})


def probe_tools_restricted(locations: list[dict]) -> dict:
    agents, n, unreadable = _own_agents(locations), 0, 0
    for a in agents.values():
        p = _real(a.get("path", ""))
        if not p.is_file():
            unreadable += 1
            continue
        fm = frontmatter(p)
        n += 1 if ("tools" in fm or "disallowedTools" in fm) else 0
    if not agents or unreadable == len(agents):
        return _rec("tools.restricted", True, "unknown", "사용자·프로젝트 에이전트 정의 파일을 읽지 못했다.")
    return _rec("tools.restricted", True, "yes" if n else "no",
                f"사용자·프로젝트 에이전트 정의 {len(agents)}개 중 {n}개가 tools 또는 disallowedTools 로 도구를 좁혔다.",
                {"agents": len(agents), "restricted": n})


def probe_bypass_mode(locations: list[dict]) -> dict:
    modes = [(L, L["enforcement"]["mode"].get("value")) for L in locations]
    if not modes or all(v is None for _, v in modes):
        return _rec("prevention.bypass_mode", True, "unknown", "시작 폴더의 권한 모드 값을 읽지 못했다.")
    bp = [L for L, v in modes if v == "bypassPermissions"]
    if not bp:
        return _rec("prevention.bypass_mode", True, "yes", f"시작 폴더 {len(modes)}곳 모두 기본 권한 모드가 bypassPermissions 가 아니다.",
                    {"locations": len(modes), "bypass": 0})
    ask = sum(p["ask"] for L in bp for p in L["enforcement"]["perm"])
    deny = sum(p["deny"] for L in bp for p in L["enforcement"]["perm"])
    return _rec("prevention.bypass_mode", True, "no",
                f"시작 폴더 {len(modes)}곳 중 {len(bp)}곳의 기본 권한 모드가 bypassPermissions 다. 이 모드에서는 승인 창과 "
                f"확인 규칙 {ask}개가 작동하지 않는다. 금지 규칙 {deny}개와 행동 전 훅은 그대로 막으므로 레벨은 내리지 않는다.",
                {"locations": len(modes), "bypass": len(bp), "ask_ignored": ask, "deny_active": deny})


def usage_u1(locations: list[dict]) -> dict:
    if (MANAGED_DIR / "managed-settings.json").exists():
        return {"value": 2, "evidence": "조직 관리 설정 파일(managed-settings.json)이 있다."}
    tracked = 0
    for L in locations:
        cwd = _real(L["cwd"])
        if not L.get("git_root"):
            continue
        for rel in (".claude/settings.json", ".claude/agents", "CLAUDE.md"):
            try:
                r = subprocess.run(["git", "-C", str(cwd), "ls-files", "--", rel], capture_output=True, text=True, timeout=5)
                tracked += 1 if r.returncode == 0 and r.stdout.strip() else 0
            except Exception:  # noqa: BLE001
                pass
    if tracked:
        return {"value": 1, "evidence": f"조직 관리 설정은 없고, 시작 폴더의 프로젝트 설정·에이전트 정의·CLAUDE.md 중 {tracked}건이 git 에 추적된다."}
    return {"value": 0, "evidence": "조직 관리 설정이 없고, 프로젝트 설정·에이전트 정의·CLAUDE.md 가 git 에 추적되지 않는다. "
                                    "git 밖에서 설정을 복사해 나눠 쓰는지는 데이터로 알 수 없다."}


# ---- history
def history_probes(locations: list[dict], H: dict, days: int, scope_note: str) -> list[dict]:
    P, span = [], f"최근 {days}일"
    has_main, has_sub = H["main_sessions"] > 0, H["sub_records"] > 0
    unk_main, unk_sub = f"{span} 메인 세션 기록이 없다.", f"{span} 서브에이전트 기록이 없다."

    # instruction.arrival
    main_md = set(_launch_md(locations))
    hit_main = sum(n for p, n in H["sub_instr_paths"].items() if p in main_md or p.replace(str(HOME), "~") in main_md)
    if not has_sub:
        P.append(_rec("instruction.arrival", True, "unknown", unk_sub))
    else:
        res = "yes" if hit_main else ("no" if H["sub_nonexplore_records"] else "unknown")
        P.append(_rec("instruction.arrival", True, res,
                      f"{span} 서브에이전트 기록 {H['sub_records']}개 중 {H['sub_instr_records']}개의 instructions 첨부에 CLAUDE.md 경로가 있고, "
                      f"메인이 읽는 CLAUDE.md 경로와 일치한 경우가 {hit_main}건이다.",
                      {"sub_records": H["sub_records"], "with_claude_md": H["sub_instr_records"], "main_path_hits": hit_main,
                       "by_agent_type": dict(H["sub_instr_by_type"])}))
    # memory.reviewed
    if not has_main:
        P.append(_rec("memory.reviewed", True, "unknown", unk_main))
    else:
        n = H["memory_edits"] + H["memory_rm"]
        P.append(_rec("memory.reviewed", True, "yes" if n else "no",
                      f"{span} 메인 기록에서 기억 경로 파일을 Edit 로 고친 호출이 {H['memory_edits']}회, rm 으로 지운 호출이 {H['memory_rm']}회이다.",
                      {"memory_edits": H["memory_edits"], "memory_rm": H["memory_rm"]}))
    # memory.subagent_receives
    if not has_sub:
        P.append(_rec("memory.subagent_receives", True, "unknown", unk_sub))
    else:
        P.append(_rec("memory.subagent_receives", True, "yes" if H["sub_automem"] else "no",
                      f"{span} 서브에이전트 기록 {H['sub_records']}개 중 {H['sub_automem']}개의 instructions 첨부에 auto memory 색인(AutoMem)이 있다."
                      + (" 공식 문서의 설명(서브에이전트는 auto memory 를 읽지 않음)과 다르다." if H["sub_automem"] else ""),
                      {"sub_records": H["sub_records"], "automem": H["sub_automem"]}))
    # tools.unused
    servers = {}
    for L in locations:
        for m in L["tools"]["mcp"]:
            if m["scope"] == "plugin":
                prefix = f"mcp__plugin_{m['source'].split('@')[0]}_{m['name']}__"
            else:
                prefix = f"mcp__{m['name']}__"
            servers.setdefault(m["name"] if m["scope"] != "plugin" else f"{m['source'].split('@')[0]}:{m['name']}", prefix)
    if not (has_main or has_sub):
        P.append(_rec("tools.unused", True, "unknown", unk_main))
    elif not servers:
        P.append(_rec("tools.unused", True, "yes", "붙여 둔 MCP 서버가 없다.", {"servers": {}}))
    else:
        use = {s: sum(n for t, n in H["tool_names"].items() if t.startswith(pre)) for s, pre in servers.items()}
        zero = sorted(s for s, n in use.items() if n == 0)
        ev = (f"{span} 메인·서브에이전트 기록에서 MCP 서버 {len(use)}개 중 {len(zero)}개가 한 번도 쓰이지 않았다({', '.join(zero)})."
              if zero else f"{span} 메인·서브에이전트 기록에서 MCP 서버 {len(use)}개가 모두 1회 이상 쓰였다.")
        P.append(_rec("tools.unused", True, "no" if zero else "yes", ev, {"calls_by_server": use}))
    # prevention.mode_drift
    set_modes = sorted({L["enforcement"]["mode"]["value"] for L in locations if L["enforcement"]["mode"]["value"]})
    teammate_bypass = sum(1 for m in H["metas"] if m.get("permissionMode") == "bypassPermissions")
    if not H["mode_sessions"]:
        P.append(_rec("prevention.mode_drift", True, "unknown", f"{span} 메인 세션에 권한 모드 기록이 없다."))
    else:
        drift = 0 if set_modes == ["bypassPermissions"] else H["bypass_sessions"]
        P.append(_rec("prevention.mode_drift", True, "no" if drift else "yes",
                      f"{span} 메인 세션 {H['main_sessions']}개 중 {H['bypass_sessions']}개가 bypassPermissions 로 실행되었다. "
                      f"설정 파일의 기본 모드는 {', '.join(set_modes) or '없음'} 이다.",
                      {"sessions": H["main_sessions"], "sessions_bypass": H["bypass_sessions"], "subagent_meta_bypass": teammate_bypass}))
    # prevention.block_seen
    pre_att = H["block_by_event"]["PreToolUse"] + H["block_by_event"]["PermissionRequest"]
    pre = pre_att + H["pre_result_blocks"]
    if not (has_main or has_sub):
        P.append(_rec("prevention.block_seen", True, "unknown", unk_main))
    else:
        P.append(_rec("prevention.block_seen", True, "yes" if pre + H["perm_denied"] else "no",
                      f"{span} 기록에서 행동 전 훅의 차단이 {pre}건(첨부 {pre_att}, 도구 결과 {H['pre_result_blocks']}), "
                      f"권한 거부로 끝난 도구 호출이 {H['perm_denied']}건이다.",
                      {"pre_hook_blocks": pre, "pre_hook_blocks_attachment": pre_att,
                       "pre_hook_blocks_result": H["pre_result_blocks"], "permission_denied": H["perm_denied"]}))
    # verification.caught
    post = sum(H["block_by_event"][e] for e in ("PostToolUse", "Stop", "SubagentStop"))
    if not (has_main or has_sub):
        P.append(_rec("verification.caught", True, "unknown", unk_main))
    else:
        by = {e: H["block_by_event"][e] for e in ("PostToolUse", "Stop", "SubagentStop") if H["block_by_event"][e]}
        P.append(_rec("verification.caught", True, "yes" if post else "no",
                      f"{span} 기록에서 행동 후·종료 시점 훅의 차단이 {post}건이고, 훅 자체의 실패(non_blocking_error)가 {H['nonblock']}건이다.",
                      {"post_blocks_by_event": by, "hook_non_blocking_error": H["nonblock"]}))
    # verification.in_subagents
    if not has_sub:
        P.append(_rec("verification.in_subagents", True, "unknown", unk_sub))
    else:
        sh = H["sub_hooks"]
        n = sh["PreToolUse"] + sh["PostToolUse"]
        P.append(_rec("verification.in_subagents", True, "yes" if n else "no",
                      f"{span} 서브에이전트 기록에서 PreToolUse 훅이 {sh['PreToolUse']}회, PostToolUse 훅이 {sh['PostToolUse']}회, "
                      f"SubagentStop 훅이 {sh['SubagentStop']}회 실행되었다.",
                      {"hooks_by_event": {k: v for k, v in sh.items() if k}}))
    # delegation.used / config_match
    own = _own_agents(locations)
    called = {n: H["agent_types"].get(n, 0) for n in own}
    meta_n = Counter(m["agentType"] for m in H["metas"] if m.get("agentType") in own)
    used = {n: max(called[n], meta_n.get(n, 0)) for n in own if max(called[n], meta_n.get(n, 0))}
    if not (has_main or has_sub):
        P.append(_rec("delegation.used", True, "unknown", unk_main))
    elif not own:
        P.append(_rec("delegation.used", True, "no", "직접 정의한 에이전트가 없다."))
    else:
        top = ", ".join(f"{k} {v}회" for k, v in sorted(used.items(), key=lambda x: -x[1])[:5])
        P.append(_rec("delegation.used", True, "yes" if used else "no",
                      f"{span} 직접 정의한 에이전트 {len(own)}개 중 {len(used)}개가 띄워졌다" + (f"({top})." if used else "."),
                      {"defined": len(own), "used": used}))
    own_meta = [m for m in H["metas"] if m.get("agentType") in own]
    mismatch = sum(1 for m in own_meta if own[m["agentType"]].get("model") not in (None, "inherit")
                   and m.get("model") and m["model"] != own[m["agentType"]]["model"])
    team_bypass = sum(1 for m in H["metas"] if m.get("taskKind") == "in_process_teammate" and m.get("permissionMode") == "bypassPermissions")
    counts = {"own_agent_records": len(own_meta), "model_mismatch": mismatch, "teammate_bypass": team_bypass}
    if mismatch or team_bypass:
        res = "no"
    elif not own_meta:
        res = "unknown"
    else:
        res = "yes"
    P.append(_rec("delegation.config_match", True, res,
                  f"{span} 서브에이전트 기록에서 직접 정의한 에이전트 기록이 {len(own_meta)}개이고 그중 모델이 정의와 다른 것이 {mismatch}개이다. "
                  f"팀원(in_process_teammate) 기록 {team_bypass}개가 bypassPermissions 로 실행되었다.", counts))
    return P


def usage_history(H: dict, days: int) -> dict:
    span = f"최근 {days}일"
    spawn = max(H["agent_calls"], len(H["metas"]))
    v2 = 0 if spawn < 10 else (1 if spawn < 90 else 2)
    d = sum(H["danger"].values())
    v3 = 0 if d == 0 else (1 if d < 10 else 2)
    pat = ", ".join(f"{k} {v}회" for k, v in H["danger"].most_common())
    return {
        "U2": {"value": v2, "evidence": f"{span} 메인 기록의 Agent 호출이 {H['agent_calls']}회, 서브에이전트 meta 기록이 {len(H['metas'])}개이다.",
               "counts": {"agent_calls": H["agent_calls"], "subagent_meta": len(H["metas"])}},
        "U3": {"value": v3, "evidence": f"{span} 명령 시작 위치에서 되돌리기 어려운 명령 패턴이 {d}회 확인되었다" + (f"({pat})." if pat else "."),
               "counts": dict(H["danger"])},
    }


def placeholder(p: dict) -> dict:
    r = {"id": p["id"], "axis": p["axis"], "method": p["method"], "by": "agent", "effect": p["effect"],
         "ran": False, "result": "unknown", "evidence": "에이전트 점검 대기", "at": _now_iso()}
    if p.get("run_if"):
        r["run_if"] = p["run_if"]
    return r


def run_probes(locations: list[dict], days: int, exclude: set[str]) -> tuple[list[dict], dict, dict]:
    dirs, scope_note = history_dirs(locations)
    H = scan_history(dirs, days, exclude)
    recs = {r["id"]: r for r in [probe_refs_live(locations), probe_index_fit(locations), probe_tools_restricted(locations),
                                    probe_bypass_mode(locations)]
            + history_probes(locations, H, days, scope_note)}
    probes = [recs[p["id"]] if p["runner"] == "script" and p["id"] in recs else placeholder(p) for p in SCORECARD["probes"]]
    at = _now_iso()
    usage = {"U1": usage_u1(locations), **usage_history(H, days)}
    for k, u in usage.items():
        d = next(x for x in SCORECARD["usage_probes"] if x["id"] == k)
        u.update({"id": k, "method": d["method"], "by": "script", "ran": True, "at": at})
    source = {"dirs": len(dirs), "scope": scope_note, "window_days": days, "main_sessions": H["main_sessions"],
              "subagent_records": H["sub_records"], "excluded_sessions": H["excluded"]}
    return probes, usage, source


# ---------------------------------------------------------------- 최종 레벨과 필요 레벨 (5장)
def final_levels(maturity: dict, probes: list[dict], usage: dict) -> None:
    """unlock4 → demote → promote5 → flag 순서. ran == false 이거나 result == unknown 인 점검은 효과가 없다."""
    def eff(axis, effect):
        return [p for p in probes if p["axis"] == axis and p["effect"] == effect and p.get("ran") and p.get("result") in ("yes", "no")]
    U = {k: (u.get("value") if u.get("ran", True) else None) for k, u in usage.items()}
    for ax in maturity["axes"]:
        a, level, applied = ax["id"], ax["auto_level"], []
        ul = [p["id"] for p in eff(a, "unlock4") if p["result"] == "yes"]
        if ul:
            lv = [4 if b["blocked_by_unknown"] == 4 else b["level"] for b in ax["by_location"]]
            new = _lower_median(lv)
            applied.append({"effect": "unlock4", "probes": ul, "from": level, "to": new})
            level = new
        dm = [p["id"] for p in eff(a, "demote") if p["result"] == "no"]
        if dm and level >= 2:
            applied.append({"effect": "demote", "probes": dm, "from": level, "to": level - 1})
            level -= 1
        pr = eff(a, "promote5")
        yes, no = [p["id"] for p in pr if p["result"] == "yes"], [p["id"] for p in pr if p["result"] == "no"]
        if level == 4 and yes and not no:
            applied.append({"effect": "promote5", "probes": yes, "from": 4, "to": 5})
            level = 5
        fl = [p["id"] for p in eff(a, "flag")]
        if fl:
            applied.append({"effect": "flag", "probes": fl})
        ax["final_level"] = level
        ax["applied"] = applied
        ax["pending"] = [p["id"] for p in probes if p["axis"] == a and (not p.get("ran") or p.get("result") == "unknown")]
        expr = SCORECARD["need_level"]["add"][a]
        names = re.findall(r"U\d", expr)
        if any(U.get(n) is None for n in names):
            ax["need_level"] = None
        else:
            add = max(U[n] for n in names)
            ax["need_level"] = min(SCORECARD["need_level"]["max"], SCORECARD["need_level"]["base"] + add)


def merge_agent_probes(probes: list[dict], usage: dict, path: str) -> int:
    """에이전트가 기록한 점검으로 같은 id 의 결과를 덮어쓴다. U1~U3 은 usage 를 덮어쓴다."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("probes", [])
    n, idx = 0, {p["id"]: i for i, p in enumerate(probes)}
    for r in data:
        pid = r.get("id")
        if pid in usage:
            usage[pid].update({"by": "agent", "at": _now_iso(), **r})
            n += 1
        elif pid in idx:
            base = probes[idx[pid]]
            probes[idx[pid]] = {**base, **r, "axis": base["axis"], "effect": base["effect"], "by": r.get("by", "agent")}
            n += 1
    return n


def probe_plan() -> str:
    out = ["에이전트 몫 점검 수행 안내 (채점표 " + SCORECARD["version"] + ")", "",
           "harness-map.py 가 할 수 없는 점검을 에이전트가 수행한다. 아래 안전 규칙을 먼저 지킨다.", "", "[안전 규칙]"]
    out += [f"{i}. {s}" for i, s in enumerate(SCORECARD["live_safety"], 1)]
    out += ["", "[점검 목록]"]
    for p in SCORECARD["probes"]:
        if p["runner"] != "agent":
            continue
        out += ["", f"- {p['id']} (축 {p['axis']}, 방법 {p['method']}, 효과 {p['effect']})", f"  확인하는 것: {p['checks']}"]
        if p.get("run_if"):
            out.append(f"  실행 조건: {p['run_if']}")
        out += [f"  절차 {i}: {s}" for i, s in enumerate(p["steps"], 1)]
        out += [f"  {k}: {v}" for k, v in p["criteria"].items()]
    u1 = next(x for x in SCORECARD["usage_probes"] if x["id"] == "U1")
    out += ["", "- U1 보충 (ask, 선택)", f"  {u1['ask_fallback']}",
            "", "[기록 형식]",
            '각 점검을 {"id", "method", "by": "agent", "ran", "result": "yes|no|unknown", "evidence", "at"} 형식의 한 항목으로 쓴다.',
            "evidence 에는 실행한 명령이나 읽은 파일과 관찰을 한두 문장으로 적고, 본문과 비밀값은 적지 않는다. at 은 ISO 8601 시각이다.",
            "U1 을 물어 답을 받았다면 {\"id\": \"U1\", \"value\": 0|1, \"evidence\": \"...\"} 형식으로 쓴다.",
            "", "[저장과 합치기]",
            "결과 배열을 harness-map-output/probes.json 에 저장한다.",
            "그다음 python3 harness-map.py --cwd <시작 폴더> ... --probes harness-map-output/probes.json -o <출력> 으로 다시 실행하면 같은 id 의 결과가 덮어쓰이고 레벨이 다시 계산된다."]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", action="append", help="시작 폴더 (여러 번 가능)")
    ap.add_argument("-o", "--out", default="-")
    ap.add_argument("--days", type=int, default=SCORECARD["history_sources"]["window_days"], help="history 점검 기간(일), 기본 30")
    ap.add_argument("--exclude-session", action="append", default=[],
                    help="집계에서 뺄 세션 id (여러 번 가능). 환경변수 CLAUDE_CODE_SESSION_ID 의 세션은 자동으로 뺀다")
    ap.add_argument("--probes", help="에이전트가 기록한 점검 결과 파일(probes.json). 같은 id 의 결과를 덮어쓰고 레벨을 다시 계산한다")
    ap.add_argument("--probe-plan", action="store_true", help="에이전트 몫 점검의 절차와 안전 규칙을 출력하고 끝낸다")
    a = ap.parse_args()
    if os.geteuid() == 0:
        raise SystemExit("root 로 실행하지 마세요. 홈이 바뀌어 다른 사람의 하네스를 읽게 됩니다. Claude Code 를 쓰는 사용자로 실행하세요.")
    if a.probe_plan:
        print(probe_plan())
        return
    cwds = [Path(c).expanduser().resolve() for c in (a.cwd or [os.getcwd()])]

    user_layer = settings_layer(CLAUDE / "settings.json", "user")
    managed_layer = settings_layer(MANAGED_DIR / "managed-settings.json", "managed")
    enabled = set(user_layer.get("enabledPlugins", []))
    for c in cwds:
        for f in ("settings.json", "settings.local.json"):
            enabled |= set(read_json(c / ".claude" / f).get("enabledPlugins", {}) or {})
    plugins = [read_plugin(pid) for pid in sorted(enabled)]
    claude_json = read_json(HOME / ".claude.json")
    try:
        ver = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10).stdout.split()[0]
    except Exception:  # noqa: BLE001
        ver = None

    locations = [assemble(c, user_layer, managed_layer, plugins, claude_json) for c in cwds]
    # 지금 실행 중인 세션은 기록이 쓰이는 중이고 점검 명령 자체가 섞이므로 뺀다(§7 history 공통 규칙).
    exclude = set(a.exclude_session) | ({os.environ["CLAUDE_CODE_SESSION_ID"]} if os.environ.get("CLAUDE_CODE_SESSION_ID") else set())
    probes, usage, source = run_probes(locations, a.days, exclude)
    merged = merge_agent_probes(probes, usage, a.probes) if a.probes else 0
    maturity = score(locations)
    final_levels(maturity, probes, usage)
    maturity["probe_source"] = source | {"agent_records_merged": merged}
    out = {
        "meta": {"tool": "harness-map", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "claude_code": ver,
                 "home": "~", "spawn_rules_source": SPAWN_RULES_DOCS},
        "unreadable": sorted(set(UNREADABLE)),
        "spawn_rules": SPAWN_RULES, "spawn_notes": SPAWN_NOTES,
        "locations": locations,
        "scorecard": SCORECARD,
        "maturity": maturity,
        "probes": probes,
        "usage": usage,
    }
    text = json.dumps(out, ensure_ascii=False, indent=1).replace(str(HOME), "~")
    text = _SECRET_RE.sub("[redacted]", text)
    if a.out == "-":
        print(text)
    else:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out} ({len(text)//1024}KB, {len(cwds)} locations, probes {len(probes)}, agent merged {merged})")


if __name__ == "__main__":
    main()
