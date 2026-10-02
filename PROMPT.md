아래 지시에 따라 내 Claude Code 하네스를 조사하고 정리해 주세요.

## 목적

하네스는 모델을 둘러싼 설정 전체를 말한다. 여기에는 지시 파일, 기억, 도구, 권한, 훅이 포함된다. 같은 사람이 같은 설정을 쓰더라도 어느 폴더에서 시작하는지, 어떤 방식으로 새 에이전트를 띄우는지에 따라 모델이 실제로 받는 내용이 달라진다. 이 작업의 목적은 그 차이를 정확하게 기록하는 것이다.

## 지켜야 할 것

1. 설정 파일은 읽기만 한다. 파일을 새로 만드는 곳은 현재 폴더 아래의 `harness-map-output/` 폴더 하나뿐이다. 그 밖의 어떤 파일도 만들거나 고치거나 지우지 않는다.
2. 비밀값을 옮겨 적지 않는다. 환경변수와 MCP 서버는 이름만 적고 값, 인자, 헤더, 토큰은 적지 않는다. 훅은 실행하는 스크립트 파일 이름만 적는다. 지시 파일은 경로와 줄 수만 적고 본문은 옮기지 않는다.
3. 홈 폴더 경로는 `~` 로 바꿔 적는다.
4. 읽을 권한이 없는 파일을 만나면 건너뛰지 말고 따로 목록에 적는다. 내가 읽을 수 없는 파일은 Claude Code도 읽을 수 없다는 뜻이므로 그 자체가 중요한 정보다.
5. 관리자 권한(sudo)으로 실행하지 않는다. 관리자 권한으로 실행하면 홈 폴더가 바뀌어 다른 사용자의 설정을 읽게 된다.
6. 확인한 사실과 추론을 구분한다. 추론에는 "[추론]" 표시를 붙인다.

## 조사 대상 폴더

내가 자주 작업을 시작하는 폴더를 물어본 뒤 진행한다. 내가 답하지 않으면 현재 폴더 하나로 진행한다. 폴더마다 아래 조사를 따로 한다. 설정은 시작 폴더에서 위쪽 폴더 방향으로 결정되므로, 상위 폴더에서 한 번 조사한 결과로 하위 프로젝트의 설정을 알 수는 없다.

## 폴더마다 조사할 것

### 1. 지시 파일 (모델이 읽고 따르려고 노력하는 내용)

- 조직 공통 지시 파일이 있는지 확인한다. macOS 는 `/Library/Application Support/ClaudeCode/CLAUDE.md`, Linux 는 `/etc/claude-code/CLAUDE.md` 이다.
- 사용자 공통 지시 파일 `~/.claude/CLAUDE.md` 와 `~/.claude/rules/` 아래의 파일을 확인한다.
- 시작 폴더와 그 위의 모든 폴더에서 `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` 를 찾는다. 이 파일들은 세션을 시작할 때 모두 이어 붙여 읽힌다.
- 시작 폴더의 `.claude/rules/` 파일을 확인한다. 파일 머리말에 `paths` 가 있으면 해당 경로의 파일을 읽을 때만 적용되므로 따로 표시한다.
- 시작 폴더 아래 하위 폴더의 `CLAUDE.md` 를 찾는다. 이 파일들은 해당 폴더의 파일을 읽을 때 뒤늦게 읽히므로 따로 표시한다.
- 각 파일의 경로, 범위(조직·사용자·프로젝트·개인·하위 폴더), 줄 수, 읽히는 시점(시작 시 또는 파일을 읽을 때)을 적는다.

### 2. 자동 기억

- 시작 폴더가 git 저장소 안에 있으면 저장소 최상위 폴더가 기억의 기준이 된다. git 저장소 밖이면 시작 폴더 자체가 기준이 된다.
- 기억 폴더는 `~/.claude/projects/<기준 폴더 경로의 / 를 - 로 바꾼 이름>/memory/` 이다. 그 안의 기억 파일 수와 `MEMORY.md` 의 줄 수를 적는다.
- 기준 폴더가 서로 다른 폴더들은 기억을 공유하지 않는다. 조사한 폴더들 사이에 기억이 나뉘어 있으면 그 사실을 적는다.

### 3. 도구

- MCP 서버를 출처별로 적는다. 사용자 공통은 `~/.claude.json` 의 `mcpServers`, 폴더 전용은 `~/.claude.json` 의 `projects["<시작 폴더 절대경로>"].mcpServers`, 프로젝트 공유는 시작 폴더의 `.mcp.json`, 플러그인 제공분은 플러그인 폴더에 있다.
- 스킬을 출처별로 적는다. 사용자 공통은 `~/.claude/skills/`, 프로젝트는 `.claude/skills/`, 플러그인 제공분은 플러그인 폴더의 `skills/` 이다.

### 4. 강제 설정 (모델의 판단과 상관없이 코드가 적용하는 설정)

- 설정 파일 네 곳을 읽는다. 조직 관리 설정(macOS 는 `/Library/Application Support/ClaudeCode/managed-settings.json`), 사용자 설정 `~/.claude/settings.json`, 프로젝트 설정 `.claude/settings.json`, 개인 설정 `.claude/settings.local.json` 이다.
- 층마다 기본 권한 모드(`permissions.defaultMode`), 허용·확인·거부 규칙의 개수, 샌드박스 사용 여부, 환경변수 이름을 적는다.
- 훅을 이벤트별로 적는다. 각 훅이 어느 설정 파일 또는 어느 플러그인에서 왔는지 함께 적는다. 플러그인 훅은 켜진 플러그인 폴더의 `hooks/hooks.json` 에 있다. 켜진 플러그인 목록은 설정 파일의 `enabledPlugins` 에 있고, 플러그인 파일은 `~/.claude/plugins/cache/<마켓 이름>/<플러그인 이름>/<버전>/` 에 있다.
- 값이 겹칠 때 어느 층이 적용되는지 적는다. 기본 권한 모드, 모델, 출력 스타일 같은 단일 값은 우선순위가 높은 층의 값이 적용된다. 우선순위는 조직 관리 설정, 실행 인자, 개인 설정, 프로젝트 설정, 사용자 설정 순서이다.

### 5. 에이전트 정의

- 에이전트 정의 파일을 출처별로 적는다. 조직 관리, 프로젝트 `.claude/agents/`, 사용자 `~/.claude/agents/`, 플러그인 `agents/` 이다.
- 이름이 같은 정의가 여러 곳에 있으면 하나만 사용된다. 사용되는 순서는 조직 관리, 실행 인자, 프로젝트, 사용자, 플러그인이다. 다른 정의에 가려져 사용되지 않는 정의를 표시한다.
- 각 정의의 모델 설정과 `omitClaudeMd`, `memory` 설정 여부를 적는다.

## 규칙의 우선순위를 정리할 때 기준

규칙의 종류마다 서로 겹칠 때 처리되는 방식이 다르다. 아래 다섯 가지 방식으로 나눠 정리한다.

1. 단일 설정값은 우선순위가 높은 층의 값 하나가 적용된다.
2. 권한 규칙은 모든 층의 규칙이 합쳐진다. 같은 동작에 허용과 거부가 함께 있으면 거부가 적용된다. 평가 순서는 훅, 거부, 확인, 권한 모드, 허용 순이다.
3. 훅은 모든 층과 플러그인의 훅이 전부 실행된다.
4. 지시 파일은 모두 이어 붙여 모델에 전달된다. 서로 충돌하는 내용이 있어도 정해진 우선순위는 없으며, 모델이 판단한다.
5. 이름이 같은 에이전트 정의는 하나만 선택된다.

## 새 에이전트를 띄울 때 전달되는 내용

아래 표는 2026년 10월 2일 기준 공식 문서(code.claude.com/docs/en/sub-agents.md, agent-teams.md, memory.md)를 정리한 것이다. 문서를 열어볼 수 있다면 다시 확인하고, 표와 다르면 문서를 따르고 차이를 적는다.

| 항목 | 포크 | 일반 서브에이전트 | 내장 Explore·Plan | 팀원 | 새 비대화형 세션(`claude -p`) |
|---|---|---|---|---|---|
| 대화 기록 | 그대로 받음 | 전달되지 않음 | 전달되지 않음 | 전달되지 않음. 띄울 때 준 지시문만 받음 | 없음 |
| 지시 파일 | 그대로 받음 | 파일에서 새로 읽음 | 읽지 않음 | 파일에서 새로 읽음 | 파일에서 새로 읽음 |
| 자동 기억 | 그대로 받음 | 문서상 읽지 않음 | 읽지 않음 | 새로 읽음 | 새로 읽음 |
| 출력 스타일 | 그대로 받음 | 전달되지 않음 | 전달되지 않음 | 전달되지 않음 | 설정대로 |
| 이미 불러온 스킬 | 그대로 받음 | 전달되지 않음. 다시 호출할 수는 있음 | 전달되지 않음 | 전달되지 않음 | 없음 |
| MCP 서버 | 그대로 받음 | 부모 것을 받음 | 부모 것을 받음 | 새로 읽음 | 새로 읽음 |
| 설정 파일의 훅 | 실행됨 | 실행됨 | 실행됨 | 실행됨 | 실행됨 |
| 권한 규칙 | 그대로 받음 | 그대로 받음 | 그대로 받음 | 그대로 받음 | 그대로 받음 |
| 권한 모드 | 그대로 받음 | 부모가 bypassPermissions·acceptEdits·auto 일 때만 부모 것을 받음 | 같음 | 리드 세션의 모드를 받음. dontAsk 는 제외 | 실행 인자로 정함 |

추가로 알아둘 사실은 다음과 같다.

- 에이전트 정의에 `omitClaudeMd: true` 가 있으면 그 에이전트는 조직 공통 지시 파일만 읽는다.
- 서브에이전트는 메인 세션 아래로 3단계까지 띄울 수 있고, 동시에 20개까지 실행된다.
- 팀원은 다른 팀원을 띄울 수 없다.
- 환경변수 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` 가 켜져 있으면, Claude 가 이름을 붙여 띄운 서브에이전트는 팀원으로 실행된다. 사용자에게 따로 묻지 않는다.
- 여러 에이전트를 한꺼번에 띄우는 오케스트레이션 플러그인과 대규모 실행도 내부적으로는 위의 방식들을 조합해 사용한다. [추론]

이 표에서 얻을 수 있는 실용적인 결론은 다음과 같다. 하위 에이전트까지 지켜야 하는 규칙은 대화로 말하지 말고 파일에 적어야 한다. 대화 중에 합의한 내용은 새로 띄운 에이전트에게 전달되지 않는다.

## 찾아서 알려줄 것

조사한 폴더마다 다음에 해당하는 상황이 있으면 쉬운 문장으로 설명한다.

- 팀 기능이 켜져 있고 기본 권한 모드가 auto·acceptEdits·bypassPermissions 중 하나인 경우. 이때는 의도하지 않아도 팀원이 생길 수 있고, 새로 띄운 에이전트가 같은 권한 모드를 받는다.
- 폴더마다 자동 기억의 기준이 달라 기억이 나뉘어 있는 경우.
- 같은 이벤트에 세 곳 이상에서 온 훅이 쌓여 있는 경우. 이 훅들은 모두 실행되며 실행 순서는 보장되지 않는다.
- 시작할 때 이어 붙여지는 지시 파일이 세 개 이상인 경우.
- 프로젝트 설정이나 개인 설정이 없어서 강제 설정이 전부 사용자 공통 설정에서 오는 경우.
- 이름이 같아서 사용되지 않는 에이전트 정의가 있는 경우.
- 읽을 권한이 없는 파일이 있는 경우.

## 출력 형식

세 가지를 차례로 만든다.

### 첫째, 사람이 읽는 요약

문어체 평서문으로 쓴다. "A는 가고 B는 남는다" 같은 대구 표어를 쓰지 않고, 무엇이 어떻게 되는지를 그대로 서술한다. 비유를 쓰지 않는다. 어려운 용어는 처음 나올 때 한 번 풀어 쓴다. 폴더마다 다섯 문장 안팎으로 정리하고, 마지막에 폴더들 사이의 차이를 정리한다.

### 둘째, JSON

harness-map 화면에 붙여 넣을 수 있도록 아래 구조를 따른다. 값을 알 수 없으면 null 로 둔다.

```json
{
  "meta": { "tool": "harness-map", "generated_at": "날짜시각", "claude_code": "버전", "home": "~",
            "spawn_rules_source": "확인한 문서와 날짜" },
  "unreadable": ["읽을 권한이 없던 파일 경로"],
  "spawn_rules": {
    "main|fork|subagent|teammate|headless": {
      "history|claude_md|rules|auto_memory|output_style|skills|mcp|hooks|perm_rules|perm_mode|model":
        "carry(그대로 받음) | reload(파일에서 새로 읽음) | cut(전달되지 않음) | cond(조건에 따라 다름)"
    }
  },
  "spawn_notes": { "fork|subagent|teammate|headless": "조건 설명 한두 문장" },
  "locations": [{
    "cwd": "~/시작 폴더", "git_root": "~/저장소 최상위 또는 null",
    "context": {
      "claude_md": [{ "scope": "managed|user|ancestor|project|local|subdir", "path": "~/...", "lines": 0, "load": "launch|on-read" }],
      "rules": [{ "scope": "user|project", "path": "~/...", "lines": 0, "load": "launch|on-read", "paths": [] }],
      "auto_memory": { "keyed_by": "git 저장소|시작 폴더 (git 밖)", "root": "~/...", "dir": "~/...", "files": 0, "index_lines": 0 },
      "output_style": { "value": null, "from": null },
      "skills": [{ "name": "", "scope": "user|project|plugin" }]
    },
    "tools": { "mcp": [{ "name": "", "scope": "user|local|project|plugin", "source": "" }] },
    "enforcement": {
      "mode": { "value": "default|acceptEdits|plan|auto|dontAsk|bypassPermissions", "from": "user|project|local|managed" },
      "model": { "value": null, "from": null },
      "perm": [{ "scope": "user", "allow": 0, "deny": 0, "ask": 0 }],
      "hooks": [{ "event": "", "scope": "", "source": "", "matcher": "*", "type": "command", "name": "스크립트 파일 이름", "blocking": true }],
      "sandbox": false, "env_keys": ["이름만"], "layers": ["user"]
    },
    "agents": [{ "scope": "", "name": "", "model": "inherit", "shadowed_by": null }],
    "plugins": [{ "id": "", "version": "", "hooks": 0, "agents": 0, "skills": 0, "commands": 0, "mcp": 0 }],
    "warnings": [{ "id": "", "level": "high|mid|low|info", "text": "쉬운 문장" }]
  }]
}
```

JSON 은 대화창에 출력하지 않고 `harness-map-output/harness.json` 파일로 저장한다.

저장하기 전에, JSON 에 비밀값처럼 보이는 문자열(긴 무작위 문자열, `sk-`, `ghp_`, `xox` 로 시작하는 값, Bearer 토큰)이나 홈 폴더의 실제 경로가 남아 있지 않은지 직접 확인한다. 남아 있으면 지우고 다시 확인한다.

### 셋째, 화면(HTML)

1. 화면 템플릿을 내려받는다. 주소는 `https://raw.githubusercontent.com/Dominic-DK/harness-map/main/template.html` 이다. 이 템플릿은 그림과 조작 화면만 담고 있고, 데이터 자리에 `__HARNESS_DATA__` 라는 표시가 하나 있다.
2. `harness.json` 의 내용을 한 줄짜리 JSON 문자열로 만든다. 이때 문자열 안의 `</` 는 모두 `<\/` 로 바꾼다. 이렇게 해야 HTML 의 script 태그가 중간에 닫히지 않는다.
3. 템플릿의 `__HARNESS_DATA__` 를 그 문자열로 바꾼다.
4. 결과를 아래 형태로 감싸 `harness-map-output/harness-map.html` 로 저장한다. 첫 줄의 doctype 과 charset 이 없으면 한글이 깨지거나 화면이 어긋날 수 있다.

```html
<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body>
(바꾼 템플릿 내용)
</body></html>
```

5. 운영체제에 맞는 명령으로 브라우저에서 연다. macOS 는 `open`, Linux 는 `xdg-open`, Windows 는 `start` 이다.
6. 템플릿을 내려받지 못하면 화면 만들기를 멈추고 그 사실을 알린다. 이 경우 템플릿을 대신할 화면을 직접 만들지 않는다. `harness.json` 은 그대로 두고, 나중에 템플릿 화면의 "내 데이터 붙여넣기" 칸에 넣어 볼 수 있다고 안내한다.

마지막에 만든 파일 두 개의 경로와, 비밀값 검사를 마쳤다는 사실을 한 줄씩 알려준다.
