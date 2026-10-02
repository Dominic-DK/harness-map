#!/usr/bin/env python3
"""harness-map — Claude Code 하네스를 읽어 '조립 지도' JSON 으로 뽑는다.

읽기만 한다. 비밀값은 남기지 않는다:
  - env, MCP 설정은 키 이름·종류만 (값·인자·헤더 제외)
  - 훅 명령은 실행 파일 이름만 (인자·경로 제외)
  - 지시 파일은 경로·줄 수만 (본문 제외)

사용:
  python3 harness-map.py [--cwd 폴더 ...] [-o 출력.json]
  --cwd 를 여러 번 주면 시작 폴더별 조립 결과를 함께 담는다 (기본: 현재 폴더).

권한: Claude Code 를 쓰는 그 사용자로 실행한다. 스크립트가 읽을 수 있는 범위 = Claude Code 가 읽는 범위다.
root 로 돌리면 홈이 바뀌어 남의 하네스를 읽는다(거부함). 읽기 권한 없는 파일은 unreadable 에 따로 남긴다.
스코프는 시작 폴더에서 위로 정해지므로, 위 폴더에서 한 번 돌리는 것으로는 아래 프로젝트 설정이 안 잡힌다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
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
                    "omitClaudeMd": fm.get("omitClaudeMd"), "has_memory": bool(fm.get("memory")), "path": str(a)})
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", action="append", help="시작 폴더 (여러 번 가능)")
    ap.add_argument("-o", "--out", default="-")
    a = ap.parse_args()
    if os.geteuid() == 0:
        raise SystemExit("root 로 실행하지 마세요. 홈이 바뀌어 다른 사람의 하네스를 읽게 됩니다. Claude Code 를 쓰는 사용자로 실행하세요.")
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

    out = {
        "meta": {"tool": "harness-map", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "claude_code": ver,
                 "home": "~", "spawn_rules_source": SPAWN_RULES_DOCS},
        "unreadable": sorted(set(UNREADABLE)),
        "spawn_rules": SPAWN_RULES, "spawn_notes": SPAWN_NOTES,
        "locations": [assemble(c, user_layer, managed_layer, plugins, claude_json) for c in cwds],
    }
    text = json.dumps(out, ensure_ascii=False, indent=1).replace(str(HOME), "~")
    if a.out == "-":
        print(text)
    else:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"wrote {a.out} ({len(text)//1024}KB, {len(cwds)} locations)")


if __name__ == "__main__":
    main()
