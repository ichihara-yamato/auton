from datetime import datetime
import html
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import textwrap
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from collections import deque
from typing import Any, Callable, Optional

import html2text
import streamlit as st
import streamlit.components.v1 as components
from openai import OpenAI
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


LLM_BASE_URL = os.getenv("AUTON_LLM_BASE_URL", "http://localhost:8000/v1")
LLM_MODEL = os.getenv("AUTON_LLM_MODEL", "bonsai-8b-v0.1.gguf")
LLM_API_KEY = os.getenv("AUTON_LLM_API_KEY", "local")
MAX_STEPS = 200
MAX_RETRIES_PER_STEP = 3
REQUEST_TIMEOUT_SECONDS = 180
ARTIFACTS_ROOT = Path(os.getenv("AUTON_ARTIFACTS_DIR", "artifacts"))
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
FIRECRAWL_BASE_URL = os.getenv("AUTON_FIRECRAWL_URL", "http://localhost:3002")
FIRECRAWL_TIMEOUT = 30
PID_DIR = Path(os.getenv("AUTON_PID_DIR", ".auton-pids"))
SERVICE_LOG_DIR = Path(os.getenv("AUTON_SERVICE_LOG_DIR", "artifacts/service-logs"))
BONSAI_START_CMD = os.getenv("AUTON_BONSAI_START_CMD", "bash ./scripts/run_prism_llama_server.sh")
FIRECRAWL_START_CMD = os.getenv("AUTON_FIRECRAWL_START_CMD", "bash ./scripts/run_firecrawl_server.sh")
FIRECRAWL_STOP_CMD = os.getenv("AUTON_FIRECRAWL_STOP_CMD", "bash ./scripts/stop_firecrawl_server.sh")
AGENT_LOG_HEIGHT_PX = int(os.getenv("AUTON_AGENT_LOG_HEIGHT_PX", "420"))


SYSTEM_PROMPT = """\
あなたは日本語で回答するシニアQAエンジニアです。
Firecrawl が取得したページ構造（Markdown）と、Playwright DOM 一覧（各要素には index / selector / cx / cy が含まれます）を
分析し、テスト指示を達成するための次の1アクションをJSONで返してください。

アクション種別:
- "click"  : 要素をクリックする。element_index 必須（dom リストの index）。
- "fill"   : 入力欄にテキストを入力する。element_index と value 必須。必ず fill を使い、click 後に keyboard.type の代わりにならない。
- "goto"   : 指定URLへナビゲートする。url 必須。
- "scroll" : ページをスクロールする。direction ("up"|"down")、amount (px、デフォルト300)。
- "assert" : ページ状態を確認する。assertion (確認内容の日本語説明), method ("css"|"text"|"url"|"title"|"count"|"auto"), target (セレクタ/テキスト/URL), passed (true|false) 必須。
           ページを観察した後、条件を満たすなら passed=true、満たさないなら passed=false で返す。
           全ページ巡回タスクでは各ページで必ず assert を呼び、テスト指示の確認項目（パンくず存在、エラーなし等）を記録する。
           passed=false の assert は失敗として記録されるが、即座には終了しない（done で終了を宣言する）。
- "done"   : テスト完了または失敗を宣言する。passed (true|false) 必須。

必ず守ること:
- 返答はJSONのみ。前後に説明文やコードブロックを書かない。
- 1回の返答は「次の1手」だけ。
- click / fill は必ず dom リストの element_index を指定する。x/y は不要（システムが selector で操作する）。
- 入力欄へのテキスト入力は必ず fill を使い、value に実際の入力値を設定する。
- ログインフォームがあれば: ① fill でメールアドレス/ID入力 → ② fill でパスワード入力 → ③ click でログインボタン押下。click だけでは絶対にログインできない。
- login_id / login_password が context に含まれる場合、その値をそのまま fill の value に使う。
- ログイン情報が空の場合はログイン操作をしない。
- page_markdown や page_text にエラー/警告が含まれる場合は assert で判定する（passed=false でテスト失敗）。
- success_criteria を満たしたら action="done", passed=true を返す。
- テスト失敗なら action="done", passed=false を返す。
- dom に目的の要素がない場合は先に scroll で表示させる。
- previous_action に「ページ遷移: A → B」と記載されていれば、そのアクションは成功している。遷移先 B が success_criteria を満たすなら即座に action="done", passed=true を返す。
- previous_action に「URL変化なし」と記載されていれば、クリックは実行されたがページ遷移は発生していない。
- 日本語の reason は簡潔に書く。

ログイン操作の典型例（dom 下記の index は一例）:
  メール入力: {"action":"fill","element_index":0,"value":"user@example.com","reason":"メール入力"}
  PW入力:     {"action":"fill","element_index":1,"value":"password123","reason":"パスワード入力"}
  ボタン:     {"action":"click","element_index":2,"reason":"ログインボタンクリック"}

返答JSONスキーマ:
{
  "reason": "次に何をするかの日本語説明",
  "action": "click|fill|goto|scroll|assert|done",
  "element_index": -1,
  "value": "",
  "url": "",
  "direction": "down",
  "amount": 300,
  "method": "css",
  "target": "",
  "assertion": "",
  "passed": true
}
"""


@dataclass
class AgentAction:
    reason: str
    action: str
    element_index: int = -1
    x: int = 0
    y: int = 0
    value: str = ""
    url: str = ""
    direction: str = "down"
    amount: int = 300
    assertion: str = ""
    method: str = "auto"     # "css"|"text"|"url"|"title"|"count"|"auto"
    target: str = ""         # selector / text / url-fragment / etc.
    passed: bool = True


REPORT_SYSTEM_PROMPT = """\
あなたは日本語で記述するシニアQAエンジニアです。テスト実行ログとメタデータをもとに、指定された3つのセクションのみをMarkdown形式で出力してください。

【重要】セクション0〜3（最終結論・原因・再現条件・エラーの影響範囲）はすでに確定済みのため出力禁止です。
ユーザーメッセージ内の "start_section_no"（N）に指定された番号から始まる以下の3セクションのみを出力してください。

## N. 修正方針
- 問題がある場合: 具体的な修正方針を記載
- 問題がない場合: 「修正不要（監視継続）」と記載

## N+1. テスト詳細結果
### テスト詳細テーブル
| URL | アサーション | 方法 | 結果 | 理由 |
|-----|------|------|------|------|
（payloadのデータから埋める）

### 巡回サマリ
（crawl_summary の discovered/visited/assertion_checked/passed/failed を箇条書き）

### 重要ログの要点
（important_logsを番号付きリストで記載）

## N+2. スクリーンショット
（screenshots の各ファイル名を `![説明](ファイル名)` 形式で。パスは含めない）

制約:
- セクション0〜3は出力禁止
- 指定された3セクション（N, N+1, N+2）のみ出力
- Markdownのみ返答（コードブロック不要）
"""

REPORT_CONTEXT_LIMIT_TOKENS = int(os.getenv("AUTON_REPORT_CONTEXT_LIMIT_TOKENS", "8192"))
REPORT_RESERVED_TOKENS = int(os.getenv("AUTON_REPORT_RESERVED_TOKENS", "800"))
REPORT_MAX_PAYLOAD_TOKENS = max(REPORT_CONTEXT_LIMIT_TOKENS - REPORT_RESERVED_TOKENS, 1000)
REPORT_LOG_TAIL_LINES_DEFAULT = int(os.getenv("AUTON_REPORT_LOG_TAIL_LINES", "220"))
REPORT_LOG_IMPORTANT_LINES_DEFAULT = int(os.getenv("AUTON_REPORT_LOG_IMPORTANT_LINES", "120"))
REPORT_SCREENSHOT_LIMIT_DEFAULT = int(os.getenv("AUTON_REPORT_SCREENSHOT_LIMIT", "28"))
REPORT_LOG_CHUNK_LINES = int(os.getenv("AUTON_REPORT_LOG_CHUNK_LINES", "80"))


CONTEXT_EXTRACT_SYSTEM_PROMPT = """\
あなたは日本語テスト指示から実行コンテキストを抽出するアシスタントです。
次のJSONのみを返してください（説明文禁止）。

{
    "start_url": "",
    "login_url": "",
    "login_id": "",
    "login_password": "",
    "test_instruction": ""
}

ルール:
- URLが1つだけでログイン文脈がなければ start_url に入れる。
- 「ログインは...」のURLは login_url に入れる。
- ログインID/メール/パスワードが文中にあれば抽出する。
- test_instruction は資格情報の記述を除いた実行指示文にする。
- 不明な項目は空文字にする。
"""


LOGIN_JUDGE_SYSTEM_PROMPT = """\
ユーザーの入力文にログイン情報（ID、メールアドレス、パスワード、ログインURL）が含まれているかを判定してください。
次のJSONのみを返してください（説明文禁止）。

{"needs_login": true}

ルール:
- 判定対象は「ユーザー入力文そのもの」のみ。Webページ側にログインリンクがあるかどうかは無関係。
- ユーザー入力文に「ログイン」「サインイン」「認証」「メールアドレス」「パスワード」などの明示語がある場合のみ true の候補。
- 上記の明示語が1つも無い場合は必ず false。
- URLが含まれているだけでは true にしない。
"""


STRATEGY_SYSTEM_PROMPT = """\
あなたはウェブテスト戦略を立案するシニアQAエンジニアです。
ユーザーの指示と現在のページ情報をもとに、タスクを完結するための戦略をJSONで返してください（説明文禁止）。

返答JSONスキーマ:
{
  "goal": "タスクの最終目標（1文）",
  "subgoals": [
    {"phase": 1, "objective": "何をするか", "success_indicator": "成功の判断基準"}
  ],
  "first_action": "scroll|goto|click|fill",
  "first_action_reason": "最初にすべきアクションの理由",
  "rollback_conditions": ["中断すべき条件"]
}

ルール:
- subgoals は最大5つ。
- 入力に needs_login=false が渡された場合、ログイン/サインイン/アカウント設定に関するフェーズを絶対に含めない。
- ニュース取得や記事要約など、ユーザー指示に直接関係する行動のみを計画する。
- first_action は現在ページで最初にとるべきアクション種別のみ。
"""


FAILURE_ANALYSIS_SYSTEM_PROMPT = """\
あなたはウェブテストの失敗を分析するシニアQAエンジニアです。
アクション、エラー内容、現在の状態をもとに根本原因と修正方針をJSONで返してください（説明文禁止）。

返答JSONスキーマ:
{
  "failure_type": "element_not_found|timeout|navigation_error|assertion_failed|other",
  "root_cause": "根本原因の説明",
  "recommended_next_action": "scroll|goto|click|fill|assert|done",
  "recommended_reason": "推奨アクションの理由",
  "modify_strategy": false,
  "abort": false,
  "abort_reason": ""
}

ルール:
- 同じ要素への繰り返しクリックが原因なら modify_strategy=true。
- リカバリ不可能な状態（ログイン必要だが情報なし等）なら abort=true。
- abort_reason は abort=true の場合のみ記述。
"""


LINK_SELECTION_SYSTEM_PROMPT = """\
あなたはリンク選定アシスタントです。
ユーザー指示とリンク候補一覧から、最も関連度が高い候補を1件だけ選んでJSONで返してください。
説明文は禁止です。

返答JSONスキーマ:
{
    "select": true,
    "candidate_index": 0,
    "url": "https://...",
    "element_index": -1,
    "reason": "選定理由",
    "score": 0.0
}

ルール:
- 候補は data 配列に含まれるものからのみ選ぶ。
- 可能な限り candidate_index で選ぶ。
- URLが現在ページと同一、または明らかに無関係な場合は select=false。
- ニュース取得系の指示なら、記事本文ページまたはニュース詳細ページを優先。
- score は 0.0-1.0。
"""


@dataclass
class TaskMemory:
    """タスク実行中の履歴・学習・戦略を保持するメモリ。"""
    plan: dict = field(default_factory=dict)
    execution_history: list = field(default_factory=list)
    learned_facts: dict = field(default_factory=dict)
    failed_attempts: list = field(default_factory=list)
    discovered_urls: set[str] = field(default_factory=set)
    visited_urls: set[str] = field(default_factory=set)
    # assertion_results: {url: [{assertion, passed, reason}]}
    assertion_results: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def record_action(self, step: int, action: "AgentAction", navigated: bool, error: Optional[str] = None) -> None:
        entry = {
            "step": step,
            "action": action.action,
            "element_index": action.element_index,
            "value": action.value,
            "url": action.url,
            "navigated": navigated,
            "error": error,
        }
        self.execution_history.append(entry)
        if error:
            self.failed_attempts.append(entry)

    def learn(self, key: str, value: Any) -> None:
        self.learned_facts[key] = value

    def to_context_dict(self, max_history: int = 5) -> dict:
        """LLMプロンプト用にメモリを圧縮して辞書で返す。"""
        return {
            "plan_goal": self.plan.get("goal", ""),
            "plan_subgoals": self.plan.get("subgoals", []),
            "recent_history": self.execution_history[-max_history:],
            "learned_facts": self.learned_facts,
            "failed_count": len(self.failed_attempts),
            "crawl_status": {
                "discovered_count": len(self.discovered_urls),
                "visited_count": len(self.visited_urls),
                "remaining_count": max(len(self.discovered_urls) - len(self.visited_urls), 0),
                "assertion_checked_count": len(self.assertion_results),
                "assertion_failed_count": sum(
                    1 for results in self.assertion_results.values()
                    if any(not r["passed"] for r in results)
                ),
            },
        }


def is_all_links_audit_task(instruction: str) -> bool:
    text = (instruction or "").lower()
    patterns = [
        r"すべてのリンク",
        r"全てのリンク",
        r"全リンク",
        r"リンクを(チェック|確認|走査)",
    ]
    has_link_scope = any(re.search(p, text) for p in patterns)
    return bool(has_link_scope)


def update_discovered_links(
    memory: TaskMemory,
    current_url: str,
    firecrawl_data: Optional[dict[str, Any]],
    dom: list[dict[str, Any]],
    mission_host: str,
) -> list[str]:
    candidates = build_link_candidates(current_url, firecrawl_data, dom)
    if mission_host:
        candidates = filter_candidates_by_domain(candidates, mission_host)
    urls: list[str] = []
    for c in candidates:
        href = str(c.get("href", "")).split("#", 1)[0]
        if href and href not in memory.discovered_urls:
            memory.discovered_urls.add(href)
            urls.append(href)
    return urls


def pop_next_unvisited_url(
    crawl_queue: deque[str],
    memory: TaskMemory,
    current_url: str,
) -> str:
    current_norm = (current_url or "").split("#", 1)[0]
    while crawl_queue:
        cand = crawl_queue.popleft().split("#", 1)[0]
        if not cand:
            continue
        if cand == current_norm:
            continue
        if cand in memory.visited_urls:
            continue
        return cand
    return ""


def init_session_state() -> None:
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("run_logs", [])
    st.session_state.setdefault("last_result", None)
    st.session_state.setdefault("last_report_md", None)
    st.session_state.setdefault("service_notice", None)


def slugify(value: str, max_length: int = 48) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    return (cleaned or "run")[:max_length]


def create_run_dir(test_instruction: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = ARTIFACTS_ROOT / f"{stamp}-{slugify(test_instruction)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def add_log(log_box: Any, message: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    st.session_state.run_logs.append(f"[{timestamp}] {message}")
    render_log_panel(log_box)


def render_log_panel(log_box: Any) -> None:
    raw = "\n".join(st.session_state.run_logs) or "まだ実行されていません。"
    escaped = html.escape(raw)
    panel_html = (
        "<html><body style='margin:0;padding:0;background:transparent;'>"
        f"<div id='auton-log-panel' style='height:{AGENT_LOG_HEIGHT_PX}px;"
        "overflow-y:auto;overflow-x:auto;border:1px solid rgba(49, 51, 63, 0.2);"
        "border-radius:6px;padding:8px;background:transparent;box-sizing:border-box;'>"
        f"<pre style='margin:0;white-space:pre;font-size:12px;line-height:1.35;"
        "font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;color:inherit;'>"
        f"{escaped}</pre></div>"
        "<script>"
        "(function(){"
        "  var el = document.getElementById('auton-log-panel');"
        "  if (!el) return;"
        "  el.scrollTop = el.scrollHeight;"
        "})();"
        "</script></body></html>"
    )
    with log_box.container():
        components.html(panel_html, height=AGENT_LOG_HEIGHT_PX + 6, scrolling=False)


def mask_secret(value: str) -> str:
    if not value:
        return "(未設定)"
    if len(value) <= 2:
        return "*" * len(value)
    return value[:1] + "*" * max(len(value) - 2, 1) + value[-1:]


def build_openai_client() -> OpenAI:
    return OpenAI(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _extract_context_by_regex(prompt: str) -> dict[str, str]:
    url_pattern = r"https?://[A-Za-z0-9.-]+(?:/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?"
    urls = re.findall(url_pattern, prompt)
    login_url = ""
    start_url = ""

    m_login_url = re.search(
        rf"ログイン(?:は|URLは|urlは)?\s*({url_pattern})",
        prompt,
        re.IGNORECASE,
    )
    if m_login_url:
        login_url = m_login_url.group(1)
    elif "ログイン" in prompt and urls:
        login_url = urls[0]

    if urls:
        if login_url and len(urls) >= 2:
            start_url = urls[1]
        elif not login_url:
            start_url = urls[0]

    m_id = re.search(
        r"(?:ログイン\s*ID|ログイン\s*id|ID|id|メールアドレス|mail|email)\s*(?:は|:|：)\s*([^\s、。]+)",
        prompt,
        re.IGNORECASE,
    )
    m_pw = re.search(r"(?:パスワード|PW)\s*(?:は|:|：)\s*([^\s、。]+)", prompt)

    return {
        "start_url": start_url,
        "login_url": login_url,
        "login_id": m_id.group(1) if m_id else "",
        "login_password": m_pw.group(1) if m_pw else "",
        "test_instruction": prompt,
    }


def extract_context_from_prompt(client: OpenAI, prompt: str) -> dict[str, str]:
    regex_ctx = _extract_context_by_regex(prompt)
    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CONTEXT_EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
    except Exception:
        return regex_ctx

    return {
        "start_url": str(data.get("start_url", "") or regex_ctx["start_url"]).strip(),
        "login_url": str(data.get("login_url", "") or regex_ctx["login_url"]).strip(),
        "login_id": str(data.get("login_id", "") or regex_ctx["login_id"]).strip(),
        "login_password": str(data.get("login_password", "") or regex_ctx["login_password"]).strip(),
        "test_instruction": str(data.get("test_instruction", "") or prompt).strip(),
    }


def llm_judge_login(client: OpenAI, prompt: str) -> bool:
    """プロンプトにログイン情報が含まれるかをLLMで yes/no 判定する。失敗時はregexにフォールバック。"""
    regex_ctx = _extract_context_by_regex(prompt)

    # チャット文面にログイン意図の明示語が無ければ、ページ側要素に引っ張られないよう即 false。
    has_login_intent = bool(
        re.search(r"ログイン|login|sign[ -]?in|サインイン|認証|パスワード|password|メールアドレス", prompt, re.IGNORECASE)
    )
    if not has_login_intent:
        return False

    # URL や資格情報が明示されている場合は LLM に委ねず deterministic に true。
    has_explicit_login_context = bool(
        regex_ctx["login_url"]
        or regex_ctx["login_id"]
        or regex_ctx["login_password"]
    )
    if has_explicit_login_context:
        return True

    # 明示語がある場合のみ、追加情報の有無を補助判定する
    has_login_evidence = bool(
        re.search(r"https?://[^\s]+/(login|signin|auth)|[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+|パスワード|password|ID\s*(は|:|：)", prompt, re.IGNORECASE)
    )
    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LOGIN_JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        llm_needs_login = bool(data.get("needs_login", False))
        # LLMが true を返しても、チャット上の証拠が弱い場合は false 側に倒す
        return bool(llm_needs_login and has_login_evidence)
    except Exception:
        return has_login_evidence


def plan_strategy(client: OpenAI, test_instruction: str, page_summary: dict, needs_login: bool = False) -> dict:
    """LLMにテスト戦略を立案させる。失敗時は最小限のデフォルト戦略を返す。"""
    user_content = json.dumps({
        "test_instruction": test_instruction,
        "needs_login": needs_login,
        "current_url": page_summary.get("url", ""),
        "page_title": page_summary.get("title", ""),
        "page_text_preview": page_summary.get("text_preview", "")[:300],
        "dom_count": page_summary.get("dom_count", 0),
    }, ensure_ascii=False)
    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": STRATEGY_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        return data if data.get("goal") else _default_strategy(test_instruction)
    except Exception:
        return _default_strategy(test_instruction)


def _default_strategy(test_instruction: str) -> dict:
    return {
        "goal": test_instruction,
        "subgoals": [{"phase": 1, "objective": test_instruction, "success_indicator": "指示が完了する"}],
        "first_action": "scroll",
        "first_action_reason": "ページ全体を確認するためスクロール",
        "rollback_conditions": [],
    }


def analyze_failure(
    client: OpenAI,
    action: "AgentAction",
    error: str,
    current_url: str,
    dom_count: int,
    memory: "TaskMemory",
) -> dict:
    """失敗アクションをLLMに分析させ、修正方針を返す。失敗時はデフォルト分析を返す。"""
    user_content = json.dumps({
        "failed_action": {
            "action": action.action,
            "element_index": action.element_index,
            "value": action.value,
            "url": action.url,
            "reason": action.reason,
        },
        "error": error,
        "current_url": current_url,
        "dom_count": dom_count,
        "failed_attempts_count": len(memory.failed_attempts),
        "recent_history": memory.execution_history[-3:],
    }, ensure_ascii=False)
    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": FAILURE_ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        return data if data.get("failure_type") else _default_failure_analysis(error)
    except Exception:
        return _default_failure_analysis(error)


def _default_failure_analysis(error: str) -> dict:
    return {
        "failure_type": "other",
        "root_cause": error,
        "recommended_next_action": "scroll",
        "recommended_reason": "エラー後の状態確認のためスクロール",
        "modify_strategy": False,
        "abort": False,
        "abort_reason": "",
    }


def build_link_candidates(
    current_url: str,
    firecrawl_data: Optional[dict[str, Any]],
    dom: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Firecrawl と DOM からリンク候補を構造化して返す。"""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(url: str, title: str, text: str, source: str, element_index: int = -1) -> None:
        u = (url or "").strip()
        if not u:
            return
        full = urllib.parse.urljoin(current_url, u)
        parsed = urllib.parse.urlparse(full)
        if parsed.scheme not in ("http", "https"):
            return
        norm = full.split("#", 1)[0]
        if norm in seen:
            return
        seen.add(norm)
        candidates.append(
            {
                "href": norm,
                "title": (title or "")[:120],
                "text": (text or "")[:120],
                "source": source,
                "element_index": element_index,
            }
        )

    for lk in (firecrawl_data or {}).get("links", [])[:30]:
        if isinstance(lk, dict):
            _add(
                lk.get("url", ""),
                lk.get("text", ""),
                lk.get("text", ""),
                "firecrawl",
            )
        elif isinstance(lk, str):
            _add(lk, "", "", "firecrawl")

    for el in dom[:80]:
        href = str(el.get("href", "") or "").strip()
        if el.get("tag") == "a" and href:
            _add(
                href,
                str(el.get("text", "")),
                str(el.get("text", "")),
                "dom",
                int(el.get("index", -1)),
            )

    return candidates[:40]


def select_relevant_link(
    client: OpenAI,
    test_instruction: str,
    current_url: str,
    candidates: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """候補リンクからユーザー指示に最も近いリンクをLLMで1件選ぶ。"""
    if not candidates:
        return None
    payload = {
        "instruction": test_instruction,
        "current_url": current_url,
        "data": candidates,
    }
    try:
        response = client.chat.completions.create(
            model=LLM_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LINK_SELECTION_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        picked = json.loads(response.choices[0].message.content or "{}")
    except Exception:
        return None

    if not picked.get("select"):
        return None

    match: Optional[dict[str, Any]] = None
    idx_raw = picked.get("candidate_index", -1)
    try:
        idx = int(idx_raw)
    except Exception:
        idx = -1
    if 0 <= idx < len(candidates):
        match = candidates[idx]
    else:
        picked_url = str(picked.get("url", "")).strip()
        if not picked_url:
            return None
        full = urllib.parse.urljoin(current_url, picked_url).split("#", 1)[0]
        match = next((c for c in candidates if c.get("href") == full), None)

    if not match:
        return None
    full = str(match.get("href", ""))
    if not full or full.split("#", 1)[0] == current_url.split("#", 1)[0]:
        return None

    return {
        "url": full,
        "element_index": int(match.get("element_index", -1)),
        "reason": str(picked.get("reason", "関連リンクを選択")),
        "score": float(picked.get("score", 0.0) or 0.0),
    }


def normalize_url(url: str, base_url: str) -> str:
    return urllib.parse.urljoin(base_url, (url or "").strip()).split("#", 1)[0]


def get_hostname(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def get_domain_scope(url: str) -> str:
    host = get_hostname(url)
    if host.startswith("www."):
        return host[4:]
    return host


def is_same_domain(url: str, base_host: str) -> bool:
    host = get_hostname(url)
    base = (base_host or "").lower()
    if not host or not base:
        return False
    return host == base or host.endswith("." + base)


def filter_candidates_by_domain(candidates: list[dict[str, Any]], base_host: str) -> list[dict[str, Any]]:
    return [c for c in candidates if is_same_domain(str(c.get("href", "")), base_host)]


def is_observed_link_url(url: str, current_url: str, candidates: list[dict[str, Any]]) -> bool:
    normalized = normalize_url(url, current_url)
    observed = {str(c.get("href", "")).split("#", 1)[0] for c in candidates}
    return normalized in observed


def _ensure_service_dirs() -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _pid_file(service_name: str) -> Path:
    return PID_DIR / f"{service_name}.pid"


def _read_pid(service_name: str) -> Optional[int]:
    pf = _pid_file(service_name)
    if not pf.exists():
        return None
    try:
        return int(pf.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _is_service_process_active(service_name: str) -> bool:
    pid = _read_pid(service_name)
    return bool(pid and _is_pid_running(pid))


def _start_service(service_name: str, command: str) -> tuple[bool, str]:
    _ensure_service_dirs()
    pid = _read_pid(service_name)
    if pid and _is_pid_running(pid):
        return True, f"すでに起動しています (pid={pid})"

    log_file = SERVICE_LOG_DIR / f"{service_name}.log"
    with log_file.open("ab") as lf:
        try:
            proc = subprocess.Popen(
                shlex.split(command),
                cwd=str(Path.cwd()),
                stdout=lf,
                stderr=lf,
                start_new_session=True,
            )
        except Exception as exc:
            return False, f"起動失敗: {type(exc).__name__}: {exc}"

    _pid_file(service_name).write_text(str(proc.pid), encoding="utf-8")
    # 起動直後に即終了していないか確認する
    time.sleep(0.4)
    exit_code = proc.poll()
    if exit_code is not None:
        _pid_file(service_name).unlink(missing_ok=True)
        return False, f"起動失敗: プロセスが終了しました (exit={exit_code})"
    return True, f"起動しました (pid={proc.pid})"


def _run_one_shot_command(command: str, timeout_sec: int = 180) -> tuple[bool, str]:
    """短時間で終わる起動/停止コマンドを実行する。"""
    try:
        res = subprocess.run(
            shlex.split(command),
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except Exception as exc:
        return False, f"コマンド実行失敗: {type(exc).__name__}: {exc}"

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        if len(err) > 300:
            err = err[-300:]
        return False, f"終了コード {res.returncode}: {err}"

    out = (res.stdout or "").strip()
    return True, out or "OK"


def _wait_service_ready(
    checker: Callable[[], tuple[bool, str]],
    timeout_sec: float,
    interval_sec: float = 0.5,
) -> tuple[bool, str]:
    deadline = time.time() + timeout_sec
    last_msg = "未接続"
    while time.time() < deadline:
        try:
            checker.clear()
        except Exception:
            pass
        ok, msg = checker()
        if ok:
            return True, msg
        last_msg = msg
        time.sleep(interval_sec)
    return False, last_msg


def _stop_service(service_name: str, fallback_patterns: list[str]) -> tuple[bool, str]:
    pid = _read_pid(service_name)
    if pid and _is_pid_running(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
            _pid_file(service_name).unlink(missing_ok=True)
            return True, f"停止しました (pid={pid})"
        except Exception as exc:
            return False, f"停止失敗: {type(exc).__name__}: {exc}"

    # pid ファイルがない/古い場合はパターンでフォールバック停止
    for pattern in fallback_patterns:
        try:
            subprocess.run(["pkill", "-f", pattern], check=False)
        except Exception:
            pass
    _pid_file(service_name).unlink(missing_ok=True)
    return True, "停止要求を送信しました"


def start_bonsai_service() -> tuple[bool, str]:
    ok, msg = _start_service("bonsai", BONSAI_START_CMD)
    if not ok:
        return False, msg
    ready, ready_msg = _wait_service_ready(check_bonsai_status, timeout_sec=30)
    if not ready:
        return False, f"起動はしたが疎通確認に失敗: {ready_msg}"
    return True, msg


def stop_bonsai_service() -> tuple[bool, str]:
    return _stop_service("bonsai", ["llama-server", "llama_cpp.server"])


def start_firecrawl_service() -> tuple[bool, str]:
    # すでに疎通可能なら起動済み扱い
    check_firecrawl_status.clear()
    ok_now, msg_now = check_firecrawl_status()
    if ok_now:
        return True, "すでに起動しています"

    ok, msg = _run_one_shot_command(FIRECRAWL_START_CMD, timeout_sec=300)
    if not ok:
        return False, f"起動失敗: {msg}"

    ready, ready_msg = _wait_service_ready(check_firecrawl_status, timeout_sec=60)
    if not ready:
        return False, f"起動はしたが疎通確認に失敗: {ready_msg}"
    return True, "起動しました"


def stop_firecrawl_service() -> tuple[bool, str]:
    ok, msg = _run_one_shot_command(FIRECRAWL_STOP_CMD, timeout_sec=180)
    # スクリプト停止に失敗したら従来フォールバック
    if not ok:
        return _stop_service("firecrawl", ["firecrawl", "localhost:3002", "3002"])

    check_firecrawl_status.clear()
    time.sleep(0.5)
    still_on, _ = check_firecrawl_status()
    if still_on:
        return False, "停止コマンド実行後も疎通可能です"
    return True, msg or "停止しました"


@st.cache_data(ttl=5, show_spinner=False)
def check_bonsai_status() -> tuple[bool, str]:
    """Bonsai(OpenAI互換)の到達性を確認する。"""
    req = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            if 200 <= resp.status < 300:
                return True, "起動中"
            return False, f"HTTP {resp.status}"
    except Exception as exc:
        return False, f"未接続 ({type(exc).__name__})"


@st.cache_data(ttl=5, show_spinner=False)
def check_firecrawl_status() -> tuple[bool, str]:
    """Firecrawl の到達性を確認する。"""
    payload = json.dumps({
        "url": "https://example.com",
        "formats": ["markdown"],
        "onlyMainContent": False,
    }).encode()
    req = urllib.request.Request(
        f"{FIRECRAWL_BASE_URL.rstrip('/')}/v1/scrape",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            if 200 <= resp.status < 300:
                return True, "起動中"
            return False, f"HTTP {resp.status}"
    except Exception as exc:
        return False, f"未接続 ({type(exc).__name__})"


def firecrawl_scrape(url: str) -> Optional[dict[str, Any]]:
    """ローカル Firecrawl でページをスクレイプ。失敗時は None を返す（フォールバック用）。"""
    payload = json.dumps({
        "url": url,
        "formats": ["markdown", "links"],
        "onlyMainContent": True,
    }).encode()
    req = urllib.request.Request(
        f"{FIRECRAWL_BASE_URL}/v1/scrape",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=FIRECRAWL_TIMEOUT) as resp:
            raw = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError):
        return None  # Firecrawl 未起動の場合はスキップ

    if not raw.get("success"):
        return None

    data = raw.get("data", {})
    # og-tag 等メタデータを除外し LLM 負荷を削減
    return {
        "markdown": (data.get("markdown") or "")[:3000],
        "links": [
            {"text": (lk if isinstance(lk, str) else lk.get("text", ""))[:60],
             "url":  (lk if isinstance(lk, str) else lk.get("url",  ""))[:120]}
            for lk in (data.get("links") or [])[:20]
        ],
    }


def take_screenshot(page: Page, run_dir: Path, name: str) -> str:
    """スクリーンショットを保存してファイルパスを返す。"""
    file_path = run_dir / f"{name}.png"
    page.screenshot(path=str(file_path), full_page=False)
    return str(file_path)


def playwright_to_markdown(page: Page, max_chars: int = 2200) -> dict[str, Any]:
    """Playwright の page.content() を html2text で Markdown に変換する。
    ヘッダー・フッター・ナビゲーションを含む全体を対象とする。"""
    try:
        html = page.content()
    except Exception:
        return {"markdown": "", "links": []}

    h = html2text.HTML2Text()
    h.ignore_images = True   # 画像は不要
    h.ignore_emphasis = False
    h.body_width = 0          # 折り返しなし
    h.protect_links = False
    h.wrap_links = False
    md = h.handle(html)[:max_chars]

    # リンク一覧を抽出（Markdown の [text](url) 形式から）
    links = [
        {"text": m.group(1)[:60], "url": m.group(2)[:120]}
        for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", md)
        if not m.group(2).startswith("#")  # アンカーリンクを除外
    ][:30]

    return {"markdown": md, "links": links}


DOM_ELEMENT_LIMIT = 80


def reduce_dom(page: Page, limit: int = DOM_ELEMENT_LIMIT) -> list[dict[str, Any]]:
    """操作可能な可視要素を selector / cx / cy 付きで返す。"""
    script = """
    (limit) => {
      const tags = ["a", "button", "input", "select", "textarea"];
      const nodes = Array.from(document.querySelectorAll(tags.join(",")));
      const visible = (el) => {
        const s = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.visibility !== "hidden" && s.display !== "none" && r.width > 0 && r.height > 0;
      };
      // 旧属性をリセット
      nodes.forEach((el) => el.removeAttribute("data-auton-index"));
      const filtered = nodes.filter((el) => visible(el)).slice(0, limit);
      filtered.forEach((el, index) => el.setAttribute("data-auton-index", String(index)));
      return filtered.map((el, index) => {
        const r = el.getBoundingClientRect();
        const id = el.id || "";
        const name = el.getAttribute("name") || "";
                const href = el.getAttribute("href") || "";
        // 常に一意な data-auton-index セレクタを selector として使う
        const selector = `[data-auton-index="${index}"]`;
        return {
          index,
          tag: el.tagName.toLowerCase(),
          id,
          name,
                    href,
          type: el.getAttribute("type") || "",
          text: (el.innerText || el.value || "").replace(/\\s+/g, " ").trim().slice(0, 100),
          aria_label: el.getAttribute("aria-label") || "",
          selector,
          cx: Math.round(r.left + r.width / 2),
          cy: Math.round(r.top + r.height / 2),
        };
      });
    }
    """
    for attempt in range(3):
        try:
            return page.evaluate(script, limit)
        except Exception as exc:
            msg = str(exc)
            transient = (
                "Execution context was destroyed" in msg
                or "Cannot find context" in msg
                or "Target page, context or browser has been closed" in msg
            )
            if not transient or attempt == 2:
                return []
            try:
                page.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            page.wait_for_timeout(200)
    return []


def parse_action(raw_text: str) -> AgentAction:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    # OpenAI-compatible local servers can still emit extra prose or a truncated wrapper
    # around the JSON object. Extract the first balanced JSON object before decoding.
    if not text.startswith("{"):
        start = text.find("{")
        if start >= 0:
            text = text[start:]

    if text.startswith("{"):
        depth = 0
        in_string = False
        escaped = False
        end_index = -1
        for index, ch in enumerate(text):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_index = index + 1
                    break

        if end_index > 0:
            text = text[:end_index]

    payload = json.loads(text)
    return AgentAction(
        reason=str(payload.get("reason", "")),
        action=str(payload.get("action", "done")).lower(),
        element_index=int(payload.get("element_index", -1)),
        x=int(payload.get("x", 0)),
        y=int(payload.get("y", 0)),
        value=str(payload.get("value", "")),
        url=str(payload.get("url", "")),
        direction=str(payload.get("direction", "down")),
        amount=int(payload.get("amount", 300)),
        assertion=str(payload.get("assertion", "")),
        method=str(payload.get("method", "auto")),
        target=str(payload.get("target", "")),
        passed=bool(payload.get("passed", True)),
    )


def _compact_dom_for_prompt(dom: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    """LLM入力用に DOM を圧縮する。"""
    compact: list[dict[str, Any]] = []
    for el in dom[:limit]:
        compact.append(
            {
                "index": el.get("index", -1),
                "tag": el.get("tag", ""),
                "type": el.get("type", ""),
                "text": str(el.get("text", ""))[:60],
                "aria_label": str(el.get("aria_label", ""))[:60],
                "id": str(el.get("id", ""))[:40],
                "name": str(el.get("name", ""))[:40],
                "href": str(el.get("href", ""))[:160],
                "cx": el.get("cx", 0),
                "cy": el.get("cy", 0),
            }
        )
    return compact


def build_user_prompt(
    test_instruction: str,
    login_url: str,
    current_url: str,
    page_title: str,
    page_text: str,
    dom: list[dict[str, Any]],
    step_no: int,
    success_criteria: str,
    login_done: bool = False,
    firecrawl_data: Optional[dict[str, Any]] = None,
    previous_page_markdown: Optional[str] = None,
    previous_error: Optional[str] = None,
    previous_action: Optional[str] = None,
    allowed_actions: Optional[list[str]] = None,
    compact_mode: bool = False,
    memory: Optional["TaskMemory"] = None,
) -> str:
    if login_done:
        login_note = "ログイン済みです。ログイン操作は不要です。"
    elif login_url:
        login_note = "ログインが必要な場合、login_urlへ遷移してください。"
    else:
        login_note = "ログイン情報は設定されていません。ログイン操作は不要です。"
    dom_limit = 20 if compact_mode else 40
    page_text_limit = 400 if compact_mode else 800
    markdown_limit = 1000 if compact_mode else 1800
    links_limit = 8 if compact_mode else 12
    context: dict[str, Any] = {
        "step": step_no,
        "current_url": current_url,
        "page_title": page_title,
        "page_text": page_text[:page_text_limit],
        "test_instruction": test_instruction,
        "success_criteria": success_criteria,
        "dom": _compact_dom_for_prompt(dom, limit=dom_limit),
        "previous_error": previous_error or "",
        "previous_action": previous_action or "",
    }
    if memory:
        context["agent_memory"] = memory.to_context_dict()
    if firecrawl_data:
        context["page_markdown"] = str(firecrawl_data.get("markdown", ""))[:markdown_limit]
        context["page_links"] = (firecrawl_data.get("links", []) or [])[:links_limit]
    if previous_page_markdown:
        context["previous_page_markdown"] = str(previous_page_markdown)[:400]
    restriction_note = ""
    if allowed_actions:
        joined = "/".join(allowed_actions)
        restriction_note = (
            f"\n【重要】前のアクションでページ遷移が発生しました。"
            f"このステップで使えるアクションは {joined} のみです。"
            f"click/fill/goto/scroll は禁止されています。"
            f"現在のページがテストの成功条件を満たすか判定してください。\n"
        )
    return (
        "Firecrawl のページ構造と DOM 要素一覧を元に次の1手を決めてください。\n"
        f"{login_note}"
        f"{restriction_note}\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}"
    )


def ask_text_llm(
    client: OpenAI,
    test_instruction: str,
    login_url: str,
    current_url: str,
    page_title: str,
    page_text: str,
    dom: list[dict[str, Any]],
    step_no: int,
    success_criteria: str,
    login_done: bool = False,
    firecrawl_data: Optional[dict[str, Any]] = None,
    previous_page_markdown: Optional[str] = None,
    previous_error: Optional[str] = None,
    previous_action: Optional[str] = None,
    allowed_actions: Optional[list[str]] = None,
    memory: Optional[TaskMemory] = None,
) -> AgentAction:
    def _call(compact: bool, json_error: Optional[str] = None) -> AgentAction:
        user_text = build_user_prompt(
            test_instruction=test_instruction,
            login_url=login_url,
            current_url=current_url,
            page_title=page_title,
            page_text=page_text,
            dom=dom,
            step_no=step_no,
            success_criteria=success_criteria,
            login_done=login_done,
            firecrawl_data=firecrawl_data,
            previous_page_markdown=previous_page_markdown,
            previous_error=previous_error,
            previous_action=previous_action,
            allowed_actions=allowed_actions,
            compact_mode=compact,
            memory=memory,
        )
        if json_error:
            user_text += (
                "\n\n前回の返答は壊れたJSONだったため失敗しました。"
                " 今回は説明文を一切付けず、1つの完全なJSONオブジェクトだけを返してください。"
                f"\n前回エラー: {json_error}"
            )
        response = client.chat.completions.create(
            model=LLM_MODEL,
            temperature=0 if json_error else 0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        )
        content = response.choices[0].message.content or ""
        return parse_action(content)

    try:
        return _call(compact=False)
    except json.JSONDecodeError as exc:
        return _call(compact=False, json_error=str(exc))
    except Exception as exc:
        if "exceeds the available context size" not in str(exc):
            raise
    # コンテキスト超過時は軽量化プロンプトで再試行
    try:
        return _call(compact=True)
    except json.JSONDecodeError as exc:
        return _call(compact=True, json_error=str(exc))


def execute_action(
    page: Page,
    action: AgentAction,
    dom: list[dict[str, Any]],
    memory: Optional[Any] = None,
    current_url: str = "",
) -> tuple[bool, bool]:
    """アクションを実行する。戻り値は (is_done, passed)。"""
    a = action.action

    def _resolve_locator():
        """element_index が有効なら selector 、なければ cx/cy でクリック土台を返す。"""
        idx = action.element_index
        if 0 <= idx < len(dom):
            return dom[idx]["selector"], dom[idx]["cx"], dom[idx]["cy"]
        return None, action.x, action.y

    def _verify_assertion(page: Page, method: str, target: str, llm_passed: bool) -> tuple[bool, str]:
        """assert の method/target に基づいてプログラム側でも検証する。
        戻り値: (verified_passed, reason)"""
        try:
            m = (method or "auto").lower()
            if m == "css" and target:
                count = page.locator(target).count()
                ok = count > 0
                return ok, f"css selector '{target}' count={count}"
            elif m == "text" and target:
                ok = target in (page.locator("body").inner_text(timeout=3000) or "")
                return ok, f"text '{target[:40]}' {'found' if ok else 'not found'}"
            elif m == "url" and target:
                ok = target in page.url
                return ok, f"url contains '{target}': {page.url}"
            elif m == "title" and target:
                ok = target in page.title()
                return ok, f"title contains '{target}': {page.title()}"
            elif m == "count" and target:
                parts = target.split(",")
                if len(parts) == 2:
                    sel, expected = parts[0].strip(), parts[1].strip()
                    count = page.locator(sel).count()
                    ok = str(count) == expected
                    return ok, f"count({sel})={count}, expected={expected}"
                return llm_passed, "count: invalid target format"
            else:
                # auto: LLMの判定をそのまま採用
                return llm_passed, "auto (LLM判定)"
        except Exception as e:
            # 検証失敗時はLLM判定にフォールバック
            return llm_passed, f"verify error: {e}"

    if a == "click":
        sel, cx, cy = _resolve_locator()
        # 座標ベースで先にクリックを試み、失敗したら selector にフォールバック
        try:
            page.mouse.click(cx, cy)
        except Exception:
            if sel:
                page.locator(sel).first.click()
            else:
                raise

    elif a == "fill":
        sel, cx, cy = _resolve_locator()
        if sel:
            page.locator(sel).first.fill(action.value)
        else:
            page.mouse.click(cx, cy, click_count=3)
            page.keyboard.type(action.value)

    elif a == "goto":
        page.goto(action.url, wait_until="domcontentloaded")

    elif a == "scroll":
        amount = action.amount or 300
        delta = amount if action.direction == "down" else -amount
        page.mouse.wheel(0, delta)

    elif a == "assert":
        url_key = current_url.split("#", 1)[0] or page.url.split("#", 1)[0]
        verified_passed, reason = _verify_assertion(page, action.method, action.target, action.passed)
        if memory is not None:
            entry = {
                "assertion": action.assertion,
                "method": action.method,
                "target": action.target,
                "passed": verified_passed,
                "reason": reason,
            }
            memory.assertion_results.setdefault(url_key, []).append(entry)
        if not verified_passed:
            raise AssertionError(f"アサーション失敗: {action.assertion} ({reason})")

    elif a == "done":
        return True, action.passed

    else:
        raise ValueError(f"未知のアクション: {action.action!r}")

    return False, True


def perform_login(
    page: Any,
    login_id: str,
    login_password: str,
    log_callback: Callable[[str], None],
) -> bool:
    """ログインページから Playwright で直接ログインする。成功時 True を返す。"""
    # ID / メールアドレス入力欄を探すオーダーで試行
    id_selectors = [
        "input[type='email']",
        "input[type='text'][name*='email']",
        "input[type='text'][name*='id']",
        "input[type='text'][name*='login']",
        "input[type='text'][name*='user']",
        "input[type='text']",
    ]
    filled_id = False
    for sel in id_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.fill(login_id)
                filled_id = True
                log_callback(f"ログイン: IDフィールド入力 ({sel})")
                break
        except Exception:
            continue

    # パスワード入力欄
    filled_pw = False
    try:
        pw_loc = page.locator("input[type='password']").first
        if pw_loc.is_visible(timeout=2000):
            pw_loc.fill(login_password)
            filled_pw = True
            log_callback("ログイン: パスワードフィールド入力")
    except Exception:
        pass

    if not filled_id or not filled_pw:
        log_callback(f"ログイン: フィールドが見つかりませんでした (id={filled_id}, pw={filled_pw})")
        return False

    # サブミットボタンを探すオーダーで試行
    submit_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('ログイン')",
        "button:has-text('Login')",
        "button:has-text('サインイン')",
    ]
    clicked = False
    for sel in submit_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=2000):
                loc.click()
                clicked = True
                log_callback(f"ログイン: ボタンクリック ({sel})")
                break
        except Exception:
            continue
    if not clicked:
        # Enter キーでフォーム送信を試みる
        try:
            page.keyboard.press("Enter")
            clicked = True
            log_callback("ログイン: Enterキーで送信")
        except Exception:
            pass
    return clicked


def run_agent(
    start_url: str,
    login_url: str,
    login_id: str,
    login_password: str,
    test_instruction: str,
    headed: bool,
    save_screenshots: bool,
    log_callback: Callable[[str], None],
    cdp_url: str = "",
) -> dict[str, Any]:
    client = build_openai_client()
    run_dir = create_run_dir(test_instruction)
    screenshots: list[str] = []
    memory = TaskMemory()

    def _crawl_summary() -> dict[str, Any]:
        visited_sorted = sorted(memory.visited_urls)
        # assertion_results: {url: [{assertion, method, target, passed, reason}]}
        failed_urls = [
            url for url, results in memory.assertion_results.items()
            if any(not r["passed"] for r in results)
        ]
        passed_urls = [
            url for url, results in memory.assertion_results.items()
            if all(r["passed"] for r in results)
        ]
        # 後方互換のためbreadcrumb_*キーも残す（assertion_resultsから集計）
        return {
            "discovered_count": len(memory.discovered_urls),
            "visited_count": len(memory.visited_urls),
            "assertion_checked_count": len(memory.assertion_results),
            "assertion_passed_count": len(passed_urls),
            "assertion_failed_count": len(failed_urls),
            "assertion_failed_urls": sorted(failed_urls),
            "assertion_results": {
                url: results
                for url, results in memory.assertion_results.items()
            },
            "visited_urls": visited_sorted,
            # 後方互換キー
            "breadcrumb_checked_count": len(memory.assertion_results),
            "breadcrumb_present_count": len(passed_urls),
            "breadcrumb_missing_count": len(failed_urls),
            "breadcrumb_missing_urls": sorted(failed_urls),
        }

    def _decorate_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
        """実行結果と品質結果を分離して返却する。既存キー(passed/status)は互換のため維持。"""
        execution_status = "success" if outcome.get("status") == "success" else "failure"
        crawl = outcome.get("crawl_summary") or {}
        checked = int(crawl.get("assertion_checked_count", 0) or 0)
        failed = int(crawl.get("assertion_failed_count", 0) or 0)

        if checked > 0:
            quality_status = "pass" if failed == 0 else "fail"
            quality_reason = (
                f"品質OK: アサーション失敗ページは検出されませんでした。({checked}件確認)"
                if quality_status == "pass"
                else f"品質NG: アサーション失敗ページが {failed} 件あります。"
            )
        else:
            quality_status = "pass" if bool(outcome.get("passed", False)) else "fail"
            quality_reason = "品質判定はテストアサーションの結果を使用しました。"

        overall_passed = execution_status == "success" and quality_status == "pass"
        outcome["execution_status"] = execution_status
        outcome["quality_status"] = quality_status
        outcome["quality_reason"] = quality_reason
        outcome["overall_passed"] = overall_passed
        return outcome

    with sync_playwright() as playwright:
        if cdp_url:
            log_callback(f"既存Chromeセッションに接続中: {cdp_url}")
            browser = playwright.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        else:
            browser = playwright.chromium.launch(headless=not headed, slow_mo=100 if headed else 0)
            page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        page.set_default_timeout(15000)

        try:
            log_callback(f"ブラウザを起動しました。artifact_dir={run_dir}")

            # ─── Phase 0a: ログイン処理 ─────────────────────────────────────
            login_done = False
            if login_url:
                log_callback(f"ログインページへ移動します: {login_url}")
                page.goto(login_url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeoutError:
                    pass
                if login_id and login_password:
                    login_shot = take_screenshot(page, run_dir, "login-before")
                    screenshots.append(login_shot)
                    log_callback("ログイン実行中 (Playwright直接操作)...")
                    login_done = perform_login(page, login_id, login_password, log_callback)
                    page.wait_for_timeout(1000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeoutError:
                        pass
                    after_login_shot = take_screenshot(page, run_dir, "login-after")
                    screenshots.append(after_login_shot)
                    log_callback(f"ログイン{'成功' if login_done else '失敗'}: 現在URL={page.url}")
                if start_url:
                    log_callback(f"テスト対象URLへ移動します: {start_url}")
                    page.goto(start_url, wait_until="domcontentloaded")
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeoutError:
                        pass
            elif start_url:
                log_callback(f"初期URLへ移動します: {start_url}")
                page.goto(start_url, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeoutError:
                    pass
            else:
                log_callback("開始URL/ログインURL が未設定のため about:blank から開始します")

            # ─── Phase 0b: 戦略立案 ──────────────────────────────────────────
            page_text_preview = ""
            try:
                page_text_preview = page.locator("body").inner_text(timeout=3000)[:300]
            except Exception:
                pass
            dom_preview = reduce_dom(page)
            page_summary = {
                "url": page.url,
                "title": page.title(),
                "text_preview": page_text_preview,
                "dom_count": len(dom_preview),
            }
            log_callback("戦略立案中...")
            has_login_context = bool(login_url or (login_id and login_password))
            memory.plan = plan_strategy(client, test_instruction, page_summary, needs_login=has_login_context)
            mission_host = get_domain_scope(start_url or page.url)
            crawl_mode = is_all_links_audit_task(test_instruction)
            crawl_queue: deque[str] = deque()
            if mission_host:
                log_callback(f"ドメイン制約: {mission_host} 配下のみ探索")
            if crawl_mode:
                log_callback("巡回モード: 全リンク監査タスクとして実行します")
            log_callback(
                f"戦略: {memory.plan.get('goal', '?')} "
                f"| サブゴール数: {len(memory.plan.get('subgoals', []))} "
                f"| 最初の手: {memory.plan.get('first_action', '?')}"
            )

            # ─── Phase 1-N: ReAct ループ ─────────────────────────────────────
            same_url_same_elem_count = 0
            last_url_elem: tuple[str, int] = ("", -1)
            last_step_action_desc: Optional[str] = None
            navigated_last_step: bool = False
            same_goto_url_count = 0
            last_goto_url = ""
            no_nav_streak = 0

            for step_no in range(1, MAX_STEPS + 1):
                shot_path = take_screenshot(page, run_dir, f"step-{step_no:02d}-before")
                screenshots.append(shot_path)

                # ── Observe ──────────────────────────────────────────────────
                firecrawl_data = firecrawl_scrape(page.url)
                if firecrawl_data:
                    log_callback(f"Step {step_no}: Firecrawl スクレイプ完了 url={page.url}")
                else:
                    firecrawl_data = playwright_to_markdown(page)
                    log_callback(f"Step {step_no}: html2text でページ構造取得 url={page.url}")

                dom = reduce_dom(page)
                page_text = ""
                try:
                    page_text = page.locator("body").inner_text(timeout=3000)[:2000]
                except Exception:
                    pass

                current_norm = page.url.split("#", 1)[0]
                memory.visited_urls.add(current_norm)

                newly_discovered = update_discovered_links(
                    memory=memory,
                    current_url=page.url,
                    firecrawl_data=firecrawl_data,
                    dom=dom,
                    mission_host=mission_host,
                )
                for u in newly_discovered:
                    if u not in memory.visited_urls:
                        crawl_queue.append(u)

                if crawl_mode:
                    remaining = max(len(memory.discovered_urls) - len(memory.visited_urls), 0)
                    log_callback(
                        f"巡回進捗: discovered={len(memory.discovered_urls)} "
                        f"visited={len(memory.visited_urls)} remaining={remaining}"
                    )
                    if remaining == 0 and step_no > 1:
                        summary = (
                            f"全リンク巡回を完了: visited={len(memory.visited_urls)} "
                            f"assertion_checked={len(memory.assertion_results)}"
                        )
                        log_callback(summary)
                        final_shot = take_screenshot(page, run_dir, f"step-{step_no:02d}-final")
                        screenshots.append(final_shot)
                        return _decorate_outcome({
                            "status": "success",
                            "message": summary,
                            "passed": True,
                            "final_url": page.url,
                            "title": page.title(),
                            "run_dir": str(run_dir),
                            "screenshots": screenshots,
                            "firecrawl_final": firecrawl_data,
                            "crawl_summary": _crawl_summary(),
                        })
                log_callback(f"Step {step_no}: DOM 取得 要素数={len(dom)}")

                loop_warning: Optional[str] = None
                previous_error: Optional[str] = None
                previous_action_desc: Optional[str] = None
                allowed_actions: Optional[list[str]] = ["assert", "done"] if navigated_last_step else None
                previous_page_markdown: Optional[str] = (
                    firecrawl_data.get("markdown") if firecrawl_data else None
                ) if step_no > 1 else None

                for retry_no in range(1, MAX_RETRIES_PER_STEP + 1):
                    forced_crawl_goto = False

                    # 巡回モードでは未訪問URLをプログラム主導で優先消化する
                    next_unvisited = ""
                    if crawl_mode and retry_no == 1:
                        next_unvisited = pop_next_unvisited_url(crawl_queue, memory, page.url)
                        if next_unvisited:
                            action = AgentAction(
                                reason="巡回キューの未訪問URLを優先遷移",
                                action="goto",
                                url=next_unvisited,
                            )
                            forced_crawl_goto = True
                            log_callback(f"巡回遷移: next={next_unvisited}")

                    # 候補リンクをLLMで選定し、ループ時や初期探索時は優先して使う
                    link_candidates_all = build_link_candidates(page.url, firecrawl_data, dom)
                    link_candidates = (
                        filter_candidates_by_domain(link_candidates_all, mission_host)
                        if mission_host
                        else link_candidates_all
                    )
                    if mission_host and not link_candidates:
                        log_callback("同一ドメインのリンク候補が見つからないため、スクロールで再探索します")
                    selected_link = None
                    if not forced_crawl_goto and (step_no <= 3 or same_goto_url_count >= 1) and link_candidates:
                        selected_link = select_relevant_link(
                            client=client,
                            test_instruction=test_instruction,
                            current_url=page.url,
                            candidates=link_candidates,
                        )

                    # ── Think ─────────────────────────────────────────────────
                    if forced_crawl_goto:
                        pass
                    elif selected_link:
                        if selected_link.get("element_index", -1) >= 0:
                            action = AgentAction(
                                reason=f"候補リンク選択(click): {selected_link.get('reason', '')}",
                                action="click",
                                element_index=int(selected_link["element_index"]),
                            )
                        else:
                            action = AgentAction(
                                reason=f"候補リンク選択(goto): {selected_link.get('reason', '')}",
                                action="goto",
                                url=str(selected_link.get("url", "")),
                            )
                        log_callback(
                            f"リンク候補選定: score={selected_link.get('score', 0):.2f} "
                            f"url={selected_link.get('url', '')}"
                        )
                    else:
                        action = ask_text_llm(
                            client=client,
                            test_instruction=test_instruction,
                            login_url=login_url,
                            current_url=page.url,
                            page_title=page.title(),
                            page_text=page_text,
                            dom=dom,
                            step_no=step_no,
                            success_criteria=test_instruction,
                            login_done=login_done,
                            firecrawl_data=firecrawl_data,
                            previous_page_markdown=previous_page_markdown,
                            previous_error=previous_error or loop_warning,
                            previous_action=previous_action_desc or last_step_action_desc,
                            allowed_actions=allowed_actions,
                            memory=memory,
                        )

                    # LLMのURL幻覚対策: 観測済み候補にない goto は実行しない
                    if action.action == "goto" and action.url and not forced_crawl_goto:
                        is_observed = is_observed_link_url(action.url, page.url, link_candidates)
                        in_domain = is_same_domain(normalize_url(action.url, page.url), mission_host) if mission_host else True
                        if (not is_observed) or (not in_domain):
                            blocked_url = action.url
                            action = AgentAction(
                                reason="候補外URLのため安全ガードでスクロールに切替",
                                action="scroll",
                                direction="down",
                                amount=450,
                            )
                            log_callback(f"gotoブロック: 候補外URL={blocked_url}")

                    # 全リンク監査タスクでは、巡回未完了なら done を受理しない
                    if crawl_mode and action.action == "done" and action.passed:
                        remaining_urls = [u for u in memory.discovered_urls if u not in memory.visited_urls]
                        next_url = pop_next_unvisited_url(crawl_queue, memory, page.url)
                        if not next_url and remaining_urls:
                            next_url = remaining_urls[0]
                        if next_url:
                            action = AgentAction(
                                reason="全リンク監査が未完了のため次の未訪問URLへ遷移",
                                action="goto",
                                url=next_url,
                            )
                            log_callback(f"done保留: 巡回未完了のため継続 url={next_url}")

                    # 外部ドメインへの click 遷移もブロック
                    if action.action == "click" and 0 <= action.element_index < len(dom) and mission_host:
                        click_href = str(dom[action.element_index].get("href", "") or "").strip()
                        if click_href:
                            full_click = normalize_url(click_href, page.url)
                            if not is_same_domain(full_click, mission_host):
                                action = AgentAction(
                                    reason="外部ドメインリンクのためclickをスキップしてスクロール",
                                    action="scroll",
                                    direction="down",
                                    amount=450,
                                )
                                log_callback(f"clickブロック: 外部URL={full_click}")

                    idx_info = (
                        f" element_index={action.element_index}"
                        if action.action in ("click", "fill") and action.element_index >= 0
                        else f" x={action.x},y={action.y}" if action.action in ("click", "fill")
                        else f" url={action.url}" if action.action == "goto"
                        else ""
                    )
                    log_callback(f"LLMアクション: [{action.action}] {action.reason}{idx_info}")

                    try:
                        # ── Act ──────────────────────────────────────────────
                        before_url = page.url
                        is_done, passed = execute_action(page, action, dom, memory=memory, current_url=page.url)

                        if is_done:
                            status = "success" if passed else "failure"
                            log_callback(f"テスト{'完了' if passed else '失敗'}: {action.reason}")
                            final_shot = take_screenshot(page, run_dir, f"step-{step_no:02d}-final")
                            screenshots.append(final_shot)
                            after_data = firecrawl_scrape(page.url)
                            return _decorate_outcome({
                                "status": status,
                                "message": action.reason,
                                "passed": passed,
                                "final_url": page.url,
                                "title": page.title(),
                                "run_dir": str(run_dir),
                                "screenshots": screenshots,
                                "firecrawl_final": after_data,
                                "crawl_summary": _crawl_summary(),
                            })

                        page.wait_for_timeout(600)
                        try:
                            page.wait_for_load_state("networkidle", timeout=5000)
                        except PlaywrightTimeoutError:
                            pass

                        # ── Observe (post-action) ────────────────────────────
                        after_data = firecrawl_scrape(page.url)
                        if after_data:
                            after_md = after_data.get("markdown", "")
                            before_md = (firecrawl_data or {}).get("markdown", "")
                            changed = after_md[:200] != before_md[:200]
                            log_callback(
                                f"アクション後検証: URL={page.url} "
                                f"ページ変化={'あり' if changed else 'なし'}"
                            )
                            previous_page_markdown = before_md
                            firecrawl_data = after_data

                        cur_elem = action.element_index if action.action == "click" else -1
                        navigated = page.url != before_url
                        nav_info = f" → ページ遷移: {before_url} → {page.url}" if navigated else " → URL変化なし"
                        previous_action_desc = (
                            f"action={action.action}, element_index={action.element_index}"
                            + (f", value={action.value!r}" if action.value else "")
                            + (f", url={action.url!r}" if action.url else "")
                            + nav_info
                        )

                        # ── メモリ更新 ────────────────────────────────────────
                        memory.record_action(step_no, action, navigated)
                        # 成功したクリックのセレクタをパターン学習
                        if action.action == "click" and 0 <= action.element_index < len(dom):
                            sel = dom[action.element_index].get("selector", "")
                            txt = dom[action.element_index].get("text", "")
                            if navigated and txt:
                                memory.learn(f"nav_click_text_{step_no}", txt[:60])

                        cur_key = (page.url, cur_elem)
                        if cur_key == last_url_elem and action.action == "click":
                            same_url_same_elem_count += 1
                        else:
                            same_url_same_elem_count = 0
                            last_url_elem = cur_key
                        if same_url_same_elem_count >= 1:
                            loop_warning = (
                                f"警告: 同じURL({page.url})で同じ要素(index={cur_elem})への"
                                f"クリックが{same_url_same_elem_count + 1}回連続しています。"
                                "テスト目標がすでに達成されている可能性があります。"
                                "現在のページがテスト指示の成功条件を満たすか確認し、満たすなら action=\"done\", passed=true を返してください。"
                            )
                        else:
                            loop_warning = None

                        # 同一 goto を繰り返しているかを検知
                        if action.action == "goto":
                            if action.url and action.url == last_goto_url and not navigated:
                                same_goto_url_count += 1
                            else:
                                same_goto_url_count = 0
                                last_goto_url = action.url
                        else:
                            same_goto_url_count = 0

                        last_step_action_desc = previous_action_desc
                        navigated_last_step = navigated
                        no_nav_streak = 0 if navigated else (no_nav_streak + 1)

                        # 停滞時は次ステップで未訪問URLへ寄せるためキュー状況をログ
                        if crawl_mode and no_nav_streak >= 2:
                            pending = max(len(memory.discovered_urls) - len(memory.visited_urls), 0)
                            log_callback(f"停滞検知: no_nav_streak={no_nav_streak}, pending={pending}")
                        log_callback(f"アクション成功: 現在URL={page.url}{nav_info}")
                        break

                    except AssertionError as exc:
                        fail_shot = take_screenshot(page, run_dir, f"step-{step_no:02d}-assert-fail")
                        screenshots.append(fail_shot)
                        log_callback(f"アサーション失敗: {exc}")
                        return _decorate_outcome({
                            "status": "failure",
                            "message": str(exc),
                            "passed": False,
                            "final_url": page.url,
                            "title": page.title(),
                            "run_dir": str(run_dir),
                            "screenshots": screenshots,
                            "crawl_summary": _crawl_summary(),
                        })

                    except Exception as exc:
                        error_str = f"{type(exc).__name__}: {exc}"
                        previous_error = error_str
                        previous_action_desc = (
                            f"action={action.action}, element_index={action.element_index}, "
                            f"value={action.value!r}, url={action.url!r}"
                        )
                        short_trace = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                        err_shot = take_screenshot(
                            page, run_dir, f"step-{step_no:02d}-retry-{retry_no}-error"
                        )
                        screenshots.append(err_shot)

                        # ── Analyze & Learn ───────────────────────────────────
                        memory.record_action(step_no, action, False, error=error_str)
                        analysis = analyze_failure(
                            client=client,
                            action=action,
                            error=error_str,
                            current_url=page.url,
                            dom_count=len(dom),
                            memory=memory,
                        )
                        log_callback(
                            f"失敗分析: type={analysis.get('failure_type')} "
                            f"cause={analysis.get('root_cause', '')[:60]} "
                            f"next={analysis.get('recommended_next_action')}"
                        )

                        # 中断すべき状態と判断された場合は即時失敗
                        if analysis.get("abort"):
                            abort_reason = analysis.get("abort_reason", "エージェントが中断を判断しました")
                            log_callback(f"エージェント中断: {abort_reason}")
                            return _decorate_outcome({
                                "status": "failure",
                                "message": abort_reason,
                                "passed": False,
                                "final_url": page.url,
                                "title": page.title(),
                                "run_dir": str(run_dir),
                                "screenshots": screenshots,
                                "crawl_summary": _crawl_summary(),
                            })

                        # 戦略修正が必要な場合はメモリに記録してプロンプトに反映
                        if analysis.get("modify_strategy"):
                            log_callback("戦略を修正します...")
                            memory.plan = plan_strategy(client, test_instruction, {
                                "url": page.url,
                                "title": page.title(),
                                "text_preview": page_text[:300],
                                "dom_count": len(dom),
                            }, needs_login=has_login_context)
                            log_callback(f"修正後の戦略: {memory.plan.get('goal', '?')}")

                        # 失敗分析の推奨アクションをヒントとして previous_error に追記
                        rec = analysis.get("recommended_next_action", "")
                        rec_reason = analysis.get("recommended_reason", "")
                        if rec:
                            previous_error = (
                                f"{error_str}\n【修正提案】次は '{rec}' を試してください: {rec_reason}"
                            )

                        # リトライ用に DOM と Firecrawl を再取得
                        dom = reduce_dom(page)
                        firecrawl_data = firecrawl_scrape(page.url) or firecrawl_data
                        try:
                            page_text = page.locator("body").inner_text(timeout=3000)[:2000]
                        except Exception:
                            pass
                        log_callback(f"操作エラー (retry {retry_no}/{MAX_RETRIES_PER_STEP}): {short_trace}")
                        if retry_no == MAX_RETRIES_PER_STEP:
                            raise RuntimeError(
                                f"ステップ {step_no} が失敗しました: {error_str}"
                            ) from exc
                else:
                    raise RuntimeError(f"ステップ {step_no} の実行に失敗しました。")

            raise RuntimeError(f"最大ステップ数 {MAX_STEPS} に到達しました。")
        finally:
            browser.close()


def render_sidebar() -> dict[str, Any]:
    st.sidebar.header("実行設定")
    headed = st.sidebar.toggle("ブラウザを表示して実行", value=False)
    save_screenshots = st.sidebar.toggle("各ステップのスクリーンショットを保存", value=True)
    cdp_url = st.sidebar.text_input(
        "既存Chromeに接続 (CDP URL)",
        value="",
        placeholder="http://localhost:9222",
        help="Chrome を --remote-debugging-port=9222 で起動した場合に入力。空欄なら新規ブラウザを起動。",
    )

    bonsai_ok, bonsai_msg = check_bonsai_status()
    firecrawl_ok, firecrawl_msg = check_firecrawl_status()
    st.sidebar.markdown("**サービス状態**")
    st.sidebar.caption(f"Bonsai 8B: {'起動中' if bonsai_ok else '停止/未接続'} ({bonsai_msg})")
    st.sidebar.caption(f"Firecrawl: {'起動中' if firecrawl_ok else '停止/未接続'} ({firecrawl_msg})")

    notice = st.session_state.get("service_notice")
    if notice:
        level, text = notice
        if level == "success":
            st.sidebar.success(text)
        else:
            st.sidebar.error(text)
        st.session_state.service_notice = None


    st.sidebar.markdown("**サービス操作**")
    bonsai_active = bonsai_ok or _is_service_process_active("bonsai")
    firecrawl_active = firecrawl_ok or _is_service_process_active("firecrawl")

    bonsai_label = "Bonsai 停止" if bonsai_active else "Bonsai 起動"
    if st.sidebar.button(bonsai_label, use_container_width=True):
        ok, msg = (stop_bonsai_service() if bonsai_active else start_bonsai_service())
        check_bonsai_status.clear()
        st.session_state.service_notice = ("success", msg) if ok else ("error", msg)
        st.rerun()

    firecrawl_label = "Firecrawl 停止" if firecrawl_active else "Firecrawl 起動"
    if st.sidebar.button(firecrawl_label, use_container_width=True):
        ok, msg = (stop_firecrawl_service() if firecrawl_active else start_firecrawl_service())
        check_firecrawl_status.clear()
        st.session_state.service_notice = ("success", msg) if ok else ("error", msg)
        st.rerun()

    # --- 新規追加: サービスWebページへのリンク ---
    st.sidebar.markdown("**サービスWebページ**")
    # Bonsai (OpenAI互換API) のWebページ（例: /docs など）
    bonsai_url = LLM_BASE_URL.rstrip("/")
    if bonsai_url.endswith(":8000/v1"):
        bonsai_url = bonsai_url[:-3]  # /v1 を除去
    bonsai_web_url = bonsai_url + "/docs"
    st.sidebar.markdown(f"[Bonsai API ドキュメント]({bonsai_web_url})", unsafe_allow_html=True)

    # Firecrawl のWebページ（例: / ルート）
    firecrawl_url = FIRECRAWL_BASE_URL.rstrip("/")
    st.sidebar.markdown(f"[Firecrawl Webページ]({firecrawl_url})", unsafe_allow_html=True)

    return {
        "headed": headed,
        "save_screenshots": save_screenshots,
        "cdp_url": cdp_url,
    }


def render_chat() -> None:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])


def generate_report_md(
    client: OpenAI,
    test_instruction: str,
    result: dict[str, Any],
    logs: list[str],
    screenshots: list[str],
) -> str:
    def _extract_important_logs(lines: list[str], max_lines: int) -> list[str]:
        if not lines:
            return []
        patterns = [
            r"失敗|エラー|Error|Exception|Traceback",
            r"完了|成功|中断|アサーション",
            r"巡回進捗|巡回遷移|done保留|停滞検知",
            r"ログイン|ドメイン制約|ブロック",
            r"レポート",
        ]
        important: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if any(re.search(p, line, re.IGNORECASE) for p in patterns):
                if line not in seen:
                    important.append(line)
                    seen.add(line)
        if len(important) > max_lines:
            return important[-max_lines:]
        return important

    def _chunk_lines(lines: list[str], chunk_size: int) -> list[list[str]]:
        if chunk_size <= 0:
            return [lines]
        return [lines[i:i + chunk_size] for i in range(0, len(lines), chunk_size)]

    def _summarize_log_chunks(lines: list[str]) -> list[dict[str, Any]]:
        chunks = _chunk_lines(lines, REPORT_LOG_CHUNK_LINES)
        summary: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks, start=1):
            if not chunk:
                continue
            text = "\n".join(chunk)
            summary.append(
                {
                    "chunk": idx,
                    "line_count": len(chunk),
                    "first": chunk[0],
                    "last": chunk[-1],
                    "error_count": len(re.findall(r"失敗|エラー|Error|Exception", text, re.IGNORECASE)),
                    "nav_count": len(re.findall(r"ページ遷移", text)),
                    "crawl_count": len(re.findall(r"巡回進捗|巡回遷移", text)),
                }
            )
        return summary

    def _pick_screenshots(paths: list[str], max_count: int) -> list[str]:
        names = [Path(p).name for p in paths]
        if len(names) <= max_count:
            return names
        must: list[str] = []
        for n in names:
            if any(k in n for k in ("error", "assert-fail", "final", "login-before", "login-after")):
                must.append(n)
        head = names[: min(5, len(names))]
        tail = names[-min(8, len(names)):]
        merged: list[str] = []
        seen: set[str] = set()
        for n in head + must + tail:
            if n not in seen:
                merged.append(n)
                seen.add(n)
        if len(merged) >= max_count:
            return merged[:max_count]
        remaining_slots = max_count - len(merged)
        stride = max(1, len(names) // max(remaining_slots, 1))
        for n in names[::stride]:
            if n not in seen:
                merged.append(n)
                seen.add(n)
            if len(merged) >= max_count:
                break
        return merged[:max_count]

    def _approx_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    def _make_payload(
        mode: str,
        logs_tail: int,
        important_limit: int,
        screenshot_limit: int,
    ) -> dict[str, Any]:
        logs_tail_data = logs[-logs_tail:] if logs_tail > 0 else []
        important_logs = _extract_important_logs(logs, important_limit)
        screenshot_names = _pick_screenshots(screenshots, screenshot_limit)
        crawl_summary = result.get("crawl_summary", {})
        failed_urls = list((crawl_summary.get("assertion_failed_urls") or [])[:10])
        for u in failed_urls:
            hint = f"[assertion-failed] url={u}"
            if hint not in important_logs:
                important_logs.append(hint)
        derived_metrics = {
            "log_lines_total": len(logs),
            "log_lines_tail": len(logs_tail_data),
            "important_log_lines": len(important_logs),
            "screenshots_total": len(screenshots),
            "screenshots_selected": len(screenshot_names),
        }
        return {
            "mode": mode,
            "test_instruction": test_instruction,
            "status": result.get("status", "unknown"),
            "passed": result.get("passed", False),
            "execution_status": result.get("execution_status", "unknown"),
            "quality_status": result.get("quality_status", "unknown"),
            "quality_reason": result.get("quality_reason", ""),
            "overall_passed": result.get("overall_passed", result.get("passed", False)),
            "final_url": result.get("final_url", ""),
            "title": result.get("title", ""),
            "message": result.get("message", ""),
            "derived_metrics": derived_metrics,
            "crawl_summary": crawl_summary,
            "important_logs": important_logs,
            "log_chunk_summaries": _summarize_log_chunks(logs_tail_data),
            "logs_tail": logs_tail_data,
            "screenshots": screenshot_names,
        }

    def _generate_factual_sections(crawl: dict[str, Any]) -> tuple[str, int]:
        """crawl_summary からセクション0〜3を確定生成。(markdown, next_section_no) を返す。"""
        checked = int(crawl.get("assertion_checked_count", 0) or 0)
        failed = int(crawl.get("assertion_failed_count", 0) or 0)
        discovered = int(crawl.get("discovered_count", 0) or 0)
        unchecked = max(0, discovered - checked)
        failed_urls: list[str] = crawl.get("assertion_failed_urls") or []
        assertion_results: dict[str, list[dict]] = crawl.get("assertion_results") or {}

        lines: list[str] = []
        lines.append("# 調査報告")
        lines.append("")
        lines.append("## 0. 最終結論")
        lines.append("")
        if checked > 0 and failed == 0:
            lines.append("- **テスト成功**：全てのアサーションがパスしました。")
        elif failed > 0:
            lines.append(f"- **テスト失敗**：{failed}件のアサーションが失敗しました。")
        else:
            lines.append("- アサーションの実行データがありません。")
        lines.append(f"- **アサーション実行数**：{checked}")
        lines.append(f"- **未アサーションページ数**：{unchecked}")
        lines.append("")

        lines.append("## 1. 原因")
        lines.append("")
        if failed > 0:
            for i, u in enumerate(failed_urls[:10], start=1):
                url_results = assertion_results.get(u, [])
                fail_items = [r for r in url_results if not r.get("passed", True)]
                for j, r in enumerate(fail_items, start=1):
                    lines.append(f"- **問題箇所{i}-{j}**：{u}")
                    lines.append(f"  - アサーション：{r.get('assertion', '')}")
                    lines.append(f"  - 方法：{r.get('method', 'auto')} / ターゲット：{r.get('target', '')}")
                    lines.append(f"  - 理由：{r.get('reason', '')}")
                    lines.append("")
        else:
            lines.append("- 問題箇所なし（検出されず）")
            lines.append("")

        lines.append("## 2. 再現条件")
        lines.append("")
        if failed > 0:
            for i, u in enumerate(failed_urls[:10], start=1):
                lines.append(f"- **条件{i}**：{u} にアクセスした場合")
                lines.append("")
        else:
            lines.append("- 再現すべき不具合なし")
            lines.append("")

        section_no = 3
        if failed > 0:
            lines.append(f"## {section_no}. エラーの影響範囲")
            lines.append("")
            lines.append(f"- **影響対象URL群**：{', '.join(failed_urls[:10])}")
            lines.append(f"- **件数**：{failed}")
            lines.append("")
            section_no += 1

        return "\n".join(lines), section_no

    def _fallback_report(payload: dict[str, Any], err: str) -> str:
        crawl = payload.get("crawl_summary") or {}
        failed = int(crawl.get("assertion_failed_count", 0) or 0)
        checked = int(crawl.get("assertion_checked_count", 0) or 0)
        shots = payload.get("screenshots", [])
        failed_set = set(crawl.get("assertion_failed_urls") or [])
        visited_urls = list(crawl.get("visited_urls") or [])
        assertion_results: dict[str, list[dict]] = crawl.get("assertion_results") or {}
        exec_ok = payload.get("execution_status", "unknown") == "success"
        quality_ok = payload.get("quality_status", "unknown") == "pass"

        # セクション0-3はテンプレート生成
        factual_md, section_no = _generate_factual_sections(crawl)
        lines: list[str] = [factual_md, ""]

        lines.append(f"## {section_no}. 修正方針")
        lines.append("")
        if failed > 0:
            lines.append(
                f"アサーション失敗ページ（{failed}件）に対して構造改善・実装修正が必要です。"
            )
        else:
            lines.append("修正不要（監視継続）")
        lines.append("")
        section_no += 1

        lines.append(f"## {section_no}. テスト詳細結果")
        lines.append("")
        lines.append("### 判定サマリ")
        lines.append("")
        lines.append(f"- 実行結果: {'成功（巡回完了）' if exec_ok else '失敗'}")
        lines.append(
            f"- 品質結果: {f'OK（アサーション全テストクリア）' if quality_ok else f'NG（アサーション失敗 {failed} 件）'}"
        )
        if payload.get("quality_reason"):
            lines.append(f"- 判定理由: {payload.get('quality_reason')}")
        lines.append("")

        lines.append("### テスト概要テーブル")
        lines.append("")
        lines.append("| URL | アサーション | 方法 | 結果 | 理由 |")
        lines.append("|-----|------|------|------|------|")
        if assertion_results:
            sorted_urls = sorted(
                assertion_results.keys(),
                key=lambda u: (all(r["passed"] for r in assertion_results[u]), u)
            )
            for u in sorted_urls:
                for r in assertion_results[u]:
                    status = "✅ 成功" if r.get("passed", True) else "❌ 失敗"
                    lines.append(
                        f"| {u} | {r.get('assertion', '')} | "
                        f"{r.get('method', 'auto')} | {status} | {r.get('reason', '')} |"
                    )
        else:
            for u in sorted(visited_urls, key=lambda x: (x not in failed_set, x)):
                row_result = "❌ 失敗" if u in failed_set else "✅ 成功"
                lines.append(f"| {u} | - | - | {row_result} | |")
        lines.append("")

        if crawl:
            lines.append("### 巡回サマリ")
            lines.append("")
            lines.append(f"- discovered: {crawl.get('discovered_count', 0)}")
            lines.append(f"- visited: {crawl.get('visited_count', 0)}")
            lines.append(f"- assertion_checked: {checked}")
            lines.append(f"- assertion_passed: {crawl.get('assertion_passed_count', 0)}")
            lines.append(f"- assertion_failed: {failed}")
            lines.append("")

        lines.append("### 重要ログ")
        lines.append("")
        for i, lg in enumerate((payload.get("important_logs") or [])[:30], start=1):
            lines.append(f"{i}. {lg}")
        lines.append("")
        section_no += 1

        lines.append(f"## {section_no}. スクリーンショット")
        lines.append("")
        for s in shots:
            lines.append(f"![{s}]({s})")
        lines.append("")
        lines.append("## 備考")
        lines.append("")
        lines.append(f"- レポート生成モード: {err}")
        return "\n".join(lines)

    payload = _make_payload(
        mode="deterministic",
        logs_tail=REPORT_LOG_TAIL_LINES_DEFAULT,
        important_limit=REPORT_LOG_IMPORTANT_LINES_DEFAULT,
        screenshot_limit=REPORT_SCREENSHOT_LIMIT_DEFAULT,
    )
    return _fallback_report(payload, "deterministic")


def create_report_zip(report_md: str, screenshots: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.md", report_md.encode("utf-8"))
        for s in screenshots:
            p = Path(s)
            if p.exists():
                zf.write(p, p.name)
    buf.seek(0)
    return buf.read()


def report_md_for_web(report_md: str) -> str:
    """Web表示用にスクリーンショット節を削除（st.image()で一覧表示をするため）。"""
    return re.sub(
        r"\n##\s+\d+\.\s*スクリーンショット\n[\s\S]*?(?=\n##\s+\d+\.\s*備考|\n##\s*備考|$)",
        "\n",
        report_md,
        flags=re.MULTILINE,
    )


def extract_screenshot_paths(report_md: str) -> list[str]:
    """Markdown中のスクリーンショットセクションから画像パスを抽出。"""
    matches = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", report_md)
    return [m for m in matches if "artifacts" in m or ".png" in m or ".jpg" in m or ".jpeg" in m]


def render_last_result() -> None:
    result = st.session_state.last_result
    if not result:
        return

    report_md = st.session_state.get("last_report_md")
    screenshots = result.get("screenshots", [])

    with st.expander("テスト結果レポート", expanded=True):
        if report_md:
            st.markdown(report_md_for_web(report_md))
            zip_bytes = create_report_zip(report_md, screenshots)
            run_dir = Path(result.get("run_dir", "run"))
            st.download_button(
                label="レポートをダウンロード (ZIP)",
                data=zip_bytes,
                file_name=f"{run_dir.name}-report.zip",
                mime="application/zip",
            )
        else:
            st.json(result)

        if screenshots:
            st.divider()
            st.caption("スクリーンショット一覧")
            cols = st.columns(3)
            for i, screenshot in enumerate(screenshots):
                screenshot_path = Path(screenshot)
                if screenshot_path.exists():
                    with cols[i % 3]:
                        st.image(str(screenshot_path), caption=screenshot_path.name, use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="Auton", layout="wide")
    init_session_state()

    st.title("Auton")
    st.caption("Bonsai 8B + Playwright で自然言語テストを自律実行する Streamlit UI")

    config = render_sidebar()

    left, right = st.columns([1.2, 1.0])

    with left:
        st.subheader("Chat")
        render_chat()
        prompt = st.chat_input("例: ログイン後にプロフィール画面へ移動し、表示名が見えることを確認してください")

    with right:
        st.subheader("Agent Logs")
        log_box = st.empty()
        render_log_panel(log_box)
        render_last_result()

    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.session_state.run_logs = []
        st.session_state.last_report_md = None
        add_log(log_box, "テスト実行を開始します。")

        try:
            parser_client = build_openai_client()

            # Phase 0: ログイン情報が含まれるか LLM で判定
            add_log(log_box, "入力を分析中...")
            needs_login = llm_judge_login(parser_client, prompt)
            add_log(log_box, f"ログイン判定: {'必要' if needs_login else '不要'}")

            if needs_login:
                # ログインが必要な場合のみ詳細抽出
                extracted = extract_context_from_prompt(parser_client, prompt)
            else:
                # ログイン不要: URLと指示のみ正規表現で十分
                extracted = _extract_context_by_regex(prompt)
                extracted["login_url"] = ""
                extracted["login_id"] = ""
                extracted["login_password"] = ""

            effective = {
                "start_url": extracted.get("start_url", ""),
                "login_url": extracted.get("login_url", ""),
                "login_id": extracted.get("login_id", ""),
                "login_password": extracted.get("login_password", ""),
                "test_instruction": extracted.get("test_instruction") or prompt,
            }

            # ログインURLと開始URLが同一なら、ログイン後の再遷移を避ける
            if effective["login_url"] and effective["start_url"]:
                if effective["login_url"].split("#", 1)[0] == effective["start_url"].split("#", 1)[0]:
                    effective["start_url"] = ""
            add_log(
                log_box,
                "入力抽出: "
                f"start_url={effective['start_url'] or '(未設定)'} / "
                f"login_url={effective['login_url'] or '(未設定)'} / "
                f"login_id={mask_secret(effective['login_id'])} / "
                f"pw={mask_secret(effective['login_password'])}",
            )

            result = run_agent(
                start_url=effective["start_url"],
                login_url=effective["login_url"],
                login_id=effective["login_id"],
                login_password=effective["login_password"],
                test_instruction=effective["test_instruction"],
                headed=config["headed"],
                save_screenshots=config["save_screenshots"],
                log_callback=lambda msg: add_log(log_box, msg),
                cdp_url=config.get("cdp_url", ""),
            )

            passed_icon = "✅" if result.get("overall_passed", result.get("passed", True)) else "❌"
            exec_label = result.get("execution_status", result.get("status", "unknown"))
            quality_label = result.get("quality_status", "unknown")
            assistant_message = textwrap.dedent(
                f"""
                テストが終了しました。{passed_icon}

                - status: `{result["status"]}`
                - execution_status: `{exec_label}`
                - quality_status: `{quality_label}`
                - message: {result["message"]}
                - final_url: `{result["final_url"]}`
                - title: `{result["title"]}`
                - artifacts: `{result["run_dir"]}`
                """
            ).strip()
            st.session_state.last_result = result
            add_log(log_box, "レポートを生成中...")
            report_client = build_openai_client()
            st.session_state.last_report_md = generate_report_md(
                client=report_client,
                test_instruction=prompt,
                result=result,
                logs=st.session_state.run_logs,
                screenshots=result.get("screenshots", []),
            )
        except Exception as exc:
            assistant_message = f"テスト実行に失敗しました: `{type(exc).__name__}: {exc}`"
            add_log(log_box, assistant_message)
            st.session_state.last_result = {"status": "error", "message": str(exc)}

        st.session_state.messages.append({"role": "assistant", "content": assistant_message})
        st.rerun()


if __name__ == "__main__":
    main()
