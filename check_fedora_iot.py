import requests
from bs4 import BeautifulSoup
import sys
import re
import os
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import time
import json

load_dotenv()

# --- Configurations ---
COMPOSE_BASE_URL = "https://kojipkgs.fedoraproject.org/compose/iot/"
OPENQA_API_URL = "https://openqa.fedoraproject.org/api/v1"
QUAY_API_URL = "https://quay.io/api/v1"
QUAY_REPOS = ["fedora/fedora-iot", "fedora/fedora-bootc"]
RETRY_COUNT = 3
RETRY_DELAY_SECONDS = 60
RUN_URL = f"https://github.com/{os.getenv('GITHUB_REPOSITORY', 'your/repo')}/actions/runs/{os.getenv('GITHUB_RUN_ID', 'local')}"

# --- AI Configuration (Claude on Vertex AI) ---
# Use same configuration as Claude Code session
VERTEX_PROJECT_ID = os.getenv("ANTHROPIC_VERTEX_PROJECT_ID", "itpc-ca-b7a2ceb3c4")
AI_MODEL = os.getenv("AI_MODEL", "claude-sonnet-4-5@20250929")
ai_client = None

try:
    from anthropic import AnthropicVertex
    # Initialize without explicit project/region - uses application default credentials
    ai_client = AnthropicVertex()
    print(f"AI configured: Claude on Vertex AI (project={VERTEX_PROJECT_ID}, model={AI_MODEL})")
except Exception as e:
    print(f"Warning: Could not configure Claude on Vertex AI: {e}. AI analysis will be disabled.")

# --- GitHub API ---
try:
    from github import Github, Auth
    MY_GITHUB_TOKEN = os.getenv("MY_GITHUB_TOKEN")
    g = Github(auth=Auth.Token(MY_GITHUB_TOKEN))
    _ = g.get_user().login
    print("GitHub client configured successfully.")
except Exception:
    from github import Github
    print("Warning: MY_GITHUB_TOKEN not found or invalid. Using unauthenticated GitHub API.")
    g = Github()

# --- Slack ---
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

# ============================================================
# PART 1: DETERMINISTIC CHECKS
# ============================================================

def get_url_content(url, silent=False):
    """Fetch text content from a URL. Set silent=True to suppress 404 error messages."""
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        if not silent:
            print(f"    -> ERROR: Could not fetch {url}. Reason: {e}")
        return None


def get_all_compose_links():
    """Fetch the compose index page with retry logic."""
    for attempt in range(RETRY_COUNT):
        try:
            print(f"Fetching compose index (attempt {attempt + 1}/{RETRY_COUNT})...")
            response = requests.get(COMPOSE_BASE_URL, timeout=60)
            response.raise_for_status()
            print("Compose index fetched successfully.")
            return BeautifulSoup(response.text, 'html.parser').find_all('a')
        except requests.exceptions.RequestException as e:
            print(f"Attempt {attempt + 1} failed: {e}")
            if attempt < RETRY_COUNT - 1:
                print(f"Retrying in {RETRY_DELAY_SECONDS} seconds...")
                time.sleep(RETRY_DELAY_SECONDS)
    print("CRITICAL: Could not fetch the compose index after multiple attempts.")
    return None


def get_fedora_release_info():
    """Query Bodhi API to determine stable and rawhide versions.

    Returns (stable_version, rawhide_version) as integers, or (None, None) on failure.
    """
    try:
        response = requests.get(
            "https://bodhi.fedoraproject.org/releases/",
            params={"exclude_archived": "true", "rows_per_page": "50"},
            headers={"Accept": "application/json"},
            timeout=30,
        )
        response.raise_for_status()

        current_versions = []
        rawhide_version = None
        for r in response.json().get("releases", []):
            if r.get("id_prefix") != "FEDORA" or not r.get("version", "").isdigit():
                continue
            ver = int(r["version"])
            if r.get("branch") == "rawhide":
                rawhide_version = ver
            elif r.get("state") == "current":
                current_versions.append(ver)

        stable = max(current_versions) if current_versions else None
        print(f"Bodhi API: stable=F{stable}, rawhide=F{rawhide_version}")
        return stable, rawhide_version
    except Exception as e:
        print(f"Warning: Could not query Bodhi API: {e}")
        return None, None


def detect_active_versions(all_links):
    """Auto-detect which Fedora IoT versions to inspect.

    Uses the Bodhi API to determine stable, then keeps stable-1 through rawhide.
    Drops anything older than stable-1.
    """
    stable, _ = get_fedora_release_info()

    cutoff_str = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y%m%d')
    version_pattern = re.compile(r'Fedora-IoT-(\d+)-(\d{8})\.\d+/')
    composed_versions = set()

    for link in all_links:
        href = link.get('href', '')
        match = version_pattern.match(href)
        if match:
            version, date_str = match.groups()
            if date_str >= cutoff_str:
                composed_versions.add(int(version))

    print(f"Versions composed in last 7 days: {sorted(composed_versions, reverse=True)}")

    if not composed_versions:
        print("No versions composed in the last 7 days.")
        return []

    if stable:
        min_version = stable - 1
        active = [v for v in composed_versions if v >= min_version]
        dropped = [v for v in composed_versions if v < min_version]
        if dropped:
            print(f"Dropped EOL versions: {sorted(dropped, reverse=True)}")
    else:
        print("Warning: Could not determine stable version, keeping top 3")
        active = sorted(composed_versions, reverse=True)[:3]

    sorted_versions = [str(v) for v in sorted(active, reverse=True)]
    print(f"Active versions to inspect: {sorted_versions}")
    return sorted_versions


def find_compose_for_date(version, all_links, date_str):
    """Find today's compose directory for a given version. Returns (compose_url, build_name)."""
    pattern = re.compile(f"Fedora-IoT-{version}-{date_str}\\.\\d+\\/")
    version_links = [link.get('href') for link in all_links if pattern.match(link.get('href', ''))]
    if not version_links:
        return None, None
    latest_compose_dir = sorted(version_links)[-1]
    build_name = latest_compose_dir.rstrip('/')
    compose_url = f"{COMPOSE_BASE_URL}{latest_compose_dir}"
    print(f"  -> Found compose: {build_name}")
    return compose_url, build_name


def check_compose_status(compose_url):
    """Check the STATUS file for a compose. Returns the status string or None."""
    status_content = get_url_content(f"{compose_url}STATUS")
    if not status_content:
        return None
    return status_content.strip()


def check_openqa_results(version, build_name):
    """Query openQA API for IoT test results."""
    results = {
        "passed": 0, "failed": 0, "softfailed": 0,
        "running": 0, "scheduled": 0,
        "failed_tests": [], "softfailed_tests": [],
        "url": None, "total": 0
    }
    if not build_name:
        return results

    try:
        response = requests.get(
            f"{OPENQA_API_URL}/jobs",
            params={"distri": "fedora", "version": version, "build": build_name, "latest": "1", "scope": "current"},
            timeout=30,
        )
        response.raise_for_status()

        for job in response.json().get("jobs", []):
            state = job.get("state", "")
            result = job.get("result", "")
            test_name = job.get("test", "unknown")
            arch = job.get("settings", {}).get("ARCH", "unknown")

            if state == "done":
                if result == "failed":
                    results["failed"] += 1
                    results["failed_tests"].append(f"{test_name} ({arch})")
                elif result == "softfailed":
                    results["softfailed"] += 1
                    results["softfailed_tests"].append(f"{test_name} ({arch})")
                elif result == "passed":
                    results["passed"] += 1
            elif state in ("running", "scheduled"):
                results[state] += 1

        results["total"] = len(response.json().get("jobs", []))
        results["url"] = (
            f"https://openqa.fedoraproject.org/tests/overview?"
            f"distri=fedora&version={version}&build={build_name}&groupid=1&groupid=5"
        )
    except Exception as e:
        print(f"    -> openQA API error: {e}")

    return results


def check_quay_container(repo, tag):
    """Check if a container tag exists on Quay.io and when it was last updated."""
    try:
        response = requests.get(f"{QUAY_API_URL}/repository/{repo}/tag/?specificTag={tag}", timeout=30)
        response.raise_for_status()
        tags = response.json().get("tags", [])
        if tags:
            return {"exists": True, "last_modified": tags[0].get("last_modified", "unknown")}
        return {"exists": False}
    except Exception as e:
        print(f"    -> Quay.io API error for {repo}:{tag}: {e}")
        return {"exists": False, "error": str(e)}


# ============================================================
# PART 2: AI FAILURE ANALYSIS
# ============================================================

def run_ai_analysis(context, prompt_instructions):
    """Run AI analysis using Claude on Vertex AI."""
    if not ai_client:
        return "AI analysis unavailable: Claude on Vertex AI not configured."

    full_prompt = f"{prompt_instructions}\n\n**Context:**\n---\n{context}\n---"
    try:
        response = ai_client.messages.create(
            model=AI_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": full_prompt}]
        )
        return response.content[0].text
    except Exception as e:
        print(f"    -> Claude analysis failed: {e}")
        return f"AI analysis failed: {e}"


def parse_ai_json(raw_text):
    """Parse JSON from AI response, stripping markdown code fences if present."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    return json.loads(cleaned)


def find_koji_task_urls(compose_url):
    """Find Koji task URLs from osbuild/ or koji-tasks/ log directories."""
    # Try osbuild/ directory (watch-task logs with embedded Koji URLs)
    print(f"    -> Checking osbuild log directory...")
    dir_content = get_url_content(f"{compose_url}logs/global/osbuild/", silent=True)
    if dir_content:
        soup = BeautifulSoup(dir_content, 'html.parser')
        koji_urls = []
        for log_link in soup.find_all('a', href=re.compile(r'IoT-\d+-watch-task\.log$')):
            log_content = get_url_content(f"{compose_url}logs/global/osbuild/{log_link['href']}")
            if log_content:
                match = re.search(r'(https://koji\.fedoraproject\.org/koji/taskinfo\?taskID=\d+)', log_content)
                if match:
                    koji_urls.append(match.group(1))
        if koji_urls:
            print(f"      -> Found {len(koji_urls)} Koji task URL(s) from osbuild logs")
            return koji_urls

    # Try koji-tasks/ directory (task ID files)
    print(f"    -> Checking koji-tasks directory...")
    dir_content = get_url_content(f"{compose_url}logs/global/koji-tasks/", silent=True)
    if dir_content:
        soup = BeautifulSoup(dir_content, 'html.parser')
        koji_urls = [
            f"https://koji.fedoraproject.org/koji/taskinfo?taskID={link['href']}"
            for link in soup.find_all('a', href=re.compile(r'^\d+$'))
        ]
        if koji_urls:
            print(f"      -> Found {len(koji_urls)} Koji task(s): {[u.split('=')[-1] for u in koji_urls]}")
            return koji_urls

    print("    -> No Koji task URLs found.")
    return []


def get_koji_task_logs(koji_task_url):
    """Extract all available log data from a Koji task page.

    Returns combined log content from compose-status.json, build.log, root.log,
    do_mounts.log, and runroot.log.
    """
    print(f"    -> Drilling into Koji Task: {koji_task_url}")
    page_content = get_url_content(koji_task_url)
    if not page_content:
        return None

    soup = BeautifulSoup(page_content, 'html.parser')

    # compose-status.json is self-contained for osbuild failures — return it directly
    json_link = soup.find('a', href=re.compile(r'.*compose-status\.json$'))
    if json_link:
        json_url = json_link['href']
        if not json_url.startswith('http'):
            json_url = "https://kojipkgs.fedoraproject.org/" + json_url
        print(f"      -> Found compose-status.json")
        content = get_url_content(json_url)
        if content:
            try:
                return json.dumps(json.loads(content), indent=2)
            except json.JSONDecodeError:
                pass

    # Collect all relevant log files (tail of each — errors are at the end)
    collected = []
    for log_name in ["build.log", "root.log", "do_mounts.log", "runroot.log"]:
        log_link = soup.find('a', string=re.compile(f'^{re.escape(log_name)}$'))
        if log_link:
            log_url = log_link['href']
            if not log_url.startswith('http'):
                log_url = "https://kojipkgs.fedoraproject.org/" + log_url
            print(f"      -> Fetching {log_name}...")
            content = get_url_content(log_url)
            if content and len(content.strip()) > 10:
                collected.append(f"=== {log_name} ===\n{content[-4000:]}")

    if collected:
        return "\n\n".join(collected)

    return None


DIAGNOSIS_PROMPT = """You are an expert Fedora IoT build engineer. You are given multiple log files from a
failed Fedora IoT compose. Your job is to identify the EXACT root cause — not a guess,
not a suggestion to "check more logs." The logs are already provided to you.

ANALYSIS RULES:
1. Read ALL provided logs before forming a conclusion.
2. deliverables.json tells you WHAT failed (which architectures, which deliverable type).
3. pungi.global.log gives the high-level flow and phase where failure occurred.
4. runroot.log / build.log / root.log contain the ACTUAL error — find the specific
   error message, traceback, or exit code.
5. For BuildrootError: identify what command failed and the exact reason (permission,
   missing file, disk space, stale mount, etc).
6. For FileNotFoundError: identify the Fedora package that provides the missing command.
7. For ostree/container errors: check for repo issues, signing problems, or ref mismatches.
8. Ignore noise: "Read-only file system" on /sys/fs/selinux/ is normal in build chroots.

YOUR RESPONSE MUST BE:
- The DEFINITIVE root cause, not speculation
- If the logs don't contain enough info, say exactly what's missing
- Actionable — tell the user what to DO, not what to investigate

You MUST respond with ONLY valid JSON, no other text:
{
  "root_cause": "definitive one-line root cause",
  "error_message": "exact error message or traceback from the logs",
  "failure_type": "IMAGE_BUILD|DEPENDENCIES|INFRASTRUCTURE|CONFIGURATION",
  "affected_arches": ["list of affected architectures"],
  "missing_packages": ["list if applicable, empty otherwise"],
  "recommended_actions": ["specific actionable steps to fix this"],
  "severity": "critical|high|medium|low",
  "needs_human_investigation": false,
  "investigation_reason": "only if needs_human_investigation is true, explain what's missing"
}"""


def collect_failure_logs(compose_url):
    """Gather all available log data from a failed compose. Returns a dict of log sources."""
    logs = {}

    # pungi.global.log (high-level overview)
    print("  --- Collecting pungi.global.log ---")
    content = get_url_content(f"{compose_url}logs/global/pungi.global.log")
    if content:
        logs["pungi.global.log"] = content[-5000:]

    # deliverables.json (what exactly failed)
    print("  --- Collecting deliverables.json ---")
    content = get_url_content(f"{compose_url}logs/global/deliverables.json", silent=True)
    if content:
        logs["deliverables.json"] = content

    # Per-architecture runroot logs (ostree-container failures)
    print("  --- Collecting per-architecture runroot logs ---")
    for arch in ["x86_64", "aarch64", "s390x", "ppc64le"]:
        content = get_url_content(
            f"{compose_url}logs/{arch}/IoT/ostree-container-1/runroot.log", silent=True
        )
        if content and len(content.strip()) > 50:
            print(f"      -> Found runroot.log for {arch} ({len(content)} bytes)")
            logs[f"runroot.log ({arch})"] = content
            break  # One arch is usually enough — same error on all

    # Koji task logs (build.log, root.log, do_mounts.log, compose-status.json)
    print("  --- Collecting Koji task logs ---")
    for koji_task_url in find_koji_task_urls(compose_url)[:3]:
        task_id = koji_task_url.split('=')[-1]
        task_logs = get_koji_task_logs(koji_task_url)
        if task_logs:
            logs[f"koji_task_{task_id}"] = task_logs
            break  # One good task is enough

    return logs


def diagnose_failure(compose_url, version_name):
    """Collect all available logs and run a single comprehensive AI analysis."""
    print(f"  Starting AI diagnosis for {version_name}...")

    logs = collect_failure_logs(compose_url)
    if not logs:
        return "_No log files found for analysis._"

    # Build combined context, budget ~15K chars total
    context_parts = []
    total_len = 0
    for source, content in logs.items():
        available = 15000 - total_len
        if available <= 500:
            break
        truncated = content[:available]
        context_parts.append(f"=== {source} ===\n{truncated}")
        total_len += len(truncated)

    print(f"  -> Collected {len(logs)} log sources, {total_len} chars total")

    # Single comprehensive AI call
    print("  --- Running AI analysis on all collected logs ---")
    raw_analysis = run_ai_analysis("\n\n".join(context_parts), DIAGNOSIS_PROMPT)
    print(f"  AI result: {raw_analysis}")

    try:
        result = parse_ai_json(raw_analysis)
    except (json.JSONDecodeError, ValueError):
        return f"*AI Diagnosis:*\n```{raw_analysis[:2500]}```"

    # Format output
    actions = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(result.get("recommended_actions", [])))
    arches = ", ".join(result.get("affected_arches", [])) or "Unknown"

    diagnosis = (
        f"*AI Diagnosis ({version_name}):*\n"
        f"> *Root cause:* {result.get('root_cause', 'Unknown')}\n"
        f"> *Error:* `{result.get('error_message', 'N/A')}`\n"
        f"> *Type:* {result.get('failure_type', 'UNKNOWN')} | *Severity:* {result.get('severity', 'unknown')}\n"
        f"> *Affected arches:* {arches}\n"
    )
    if result.get("missing_packages"):
        diagnosis += f"> *Missing packages:* {', '.join(result['missing_packages'])}\n"
    diagnosis += f"> *Actions:*\n{actions}"
    if result.get("needs_human_investigation"):
        diagnosis += f"\n> :mag: *Needs investigation:* {result.get('investigation_reason', '')}"

    return diagnosis


# ============================================================
# OUTPUT: SLACK NOTIFICATION
# ============================================================

def send_slack_notification(blocks):
    """Send a structured Slack message, or print to stdout if no webhook configured."""
    for block in blocks:
        if block.get("type") == "section":
            text = block.get("text", {}).get("text", "")
            if len(text) > 2900:
                block["text"]["text"] = text[:2900] + "\n... _(truncated)_"

    if not SLACK_WEBHOOK_URL:
        print("\n" + "=" * 60)
        print("SLACK PREVIEW (SLACK_WEBHOOK_URL not set)")
        print("=" * 60)
        for block in blocks:
            btype = block.get("type")
            if btype == "header":
                print(f"\n  {block['text']['text']}")
                print("  " + "-" * 40)
            elif btype == "section":
                print(f"\n{block['text']['text']}")
            elif btype == "context":
                for el in block.get("elements", []):
                    print(f"  {el.get('text', '')}")
            elif btype == "divider":
                print("-" * 40)
        print("=" * 60)
        return

    print("Sending summary to Slack...")
    try:
        response = requests.post(
            SLACK_WEBHOOK_URL,
            data=json.dumps({"blocks": blocks}),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        response.raise_for_status()
        print("Slack notification sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"Error sending Slack notification: {e}")


def format_slack_blocks(date_str, version_reports):
    """Build structured Slack blocks from version reports."""
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Fedora IoT Compose Report - {date_str}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{RUN_URL}|GitHub Actions run log>"}]},
        {"type": "divider"},
    ]

    status_emojis = {
        "FINISHED": "white_check_mark", "FINISHED_INCOMPLETE": "warning",
        "DOOMED": "fire", "STARTED": "hourglass_flowing_sand", "MISSING": "x",
    }

    for report in version_reports:
        emoji = status_emojis.get(report["status"], "question")
        text = f":{emoji}: *Fedora IoT {report['version']}* — `{report['status']}`"

        if report.get("compose_url"):
            text += f"\n<{report['compose_url']}|Compose directory>"

        # openQA
        oqa = report.get("openqa")
        if oqa and oqa.get("total", 0) > 0:
            oqa_line = f"\n*openQA:* {oqa['passed']} passed"
            if oqa["failed"]:
                oqa_line += f", *{oqa['failed']} failed*"
            if oqa["softfailed"]:
                oqa_line += f", {oqa['softfailed']} softfailed"
            if oqa["running"]:
                oqa_line += f", {oqa['running']} running"
            if oqa.get("url"):
                oqa_line += f" — <{oqa['url']}|View tests>"
            text += oqa_line
            if oqa["failed_tests"]:
                text += f"\n:x: *Failed:* {', '.join(oqa['failed_tests'])}"
            if oqa["softfailed_tests"]:
                shown = ', '.join(oqa['softfailed_tests'][:5])
                extra = len(oqa['softfailed_tests']) - 5
                text += f"\n:warning: *Softfailed:* {shown}"
                if extra > 0:
                    text += f" + {extra} more"

        # Containers
        if report.get("containers"):
            parts = []
            for repo_name, info in report["containers"].items():
                short = repo_name.split('/')[-1]
                if info.get("exists"):
                    parts.append(f"{short} (updated {info['last_modified'][:16]})")
                else:
                    parts.append(f"*{short} MISSING*")
            text += f"\n*Containers:* {', '.join(parts)}"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

        if report.get("diagnosis"):
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": report["diagnosis"]}})

        blocks.append({"type": "divider"})

    return blocks


# ============================================================
# MAIN
# ============================================================

def inspect_version(version, all_links, current_date_str):
    """Run all checks for a single version. Returns a report dict."""
    print(f"\n{'='*40}")
    print(f"Inspecting Fedora IoT {version}...")

    report = {"version": version, "status": "MISSING", "compose_url": None}

    compose_url, build_name = find_compose_for_date(version, all_links, current_date_str)
    if not compose_url:
        print(f"  -> No compose found for {current_date_str}")
        return report

    report["compose_url"] = compose_url

    status = check_compose_status(compose_url)
    if not status:
        report["status"] = "UNKNOWN"
        return report
    report["status"] = status
    print(f"  -> Status: {status}")

    print(f"  -> Checking openQA results...")
    report["openqa"] = check_openqa_results(version, build_name)
    oqa = report["openqa"]
    print(f"     {oqa['passed']} passed, {oqa['failed']} failed, {oqa['softfailed']} softfailed")

    print(f"  -> Checking Quay.io containers...")
    report["containers"] = {repo: check_quay_container(repo, version) for repo in QUAY_REPOS}

    if status in ("DOOMED", "FINISHED_INCOMPLETE"):
        if ai_client:
            report["diagnosis"] = diagnose_failure(compose_url, f"Fedora-IoT-{version}")
        else:
            report["diagnosis"] = (
                f":information_source: *Manual investigation needed*\n"
                f"Check logs at: <{compose_url}logs/|Compose logs directory>"
            )

    return report


def save_daily_report(date_str, reports):
    """Save the daily report as a JSON artifact for meeting prep aggregation."""
    artifact_path = os.getenv("GITHUB_WORKSPACE", ".")
    report_file = os.path.join(artifact_path, f"daily-report-{date_str}.json")
    with open(report_file, "w") as f:
        json.dump({
            "date": date_str,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "versions": reports,
        }, f, indent=2, default=str)
    print(f"Daily report saved to {report_file}")


def run_diagnose(build_name):
    """Run AI diagnosis on a specific compose build (e.g. Fedora-IoT-42-20260427.0)."""
    if not ai_client:
        print("ERROR: AI client not configured. Run 'gcloud auth application-default login' first.")
        sys.exit(1)

    compose_url = f"{COMPOSE_BASE_URL}{build_name}/"
    status = check_compose_status(compose_url)
    if not status:
        print(f"ERROR: Could not find compose at {compose_url}")
        sys.exit(1)

    print(f"Compose: {build_name} (status: {status})")
    result = diagnose_failure(compose_url, build_name)
    print("\n" + "=" * 60)
    print(result)
    print("=" * 60)


def main():
    # Handle --diagnose mode
    if len(sys.argv) >= 3 and sys.argv[1] == "--diagnose":
        run_diagnose(sys.argv[2])
        return

    print("Starting Fedora IoT Compose Inspection")
    print("=" * 50)

    # Allow date override for testing
    override_date = os.getenv("OVERRIDE_DATE")
    if override_date:
        current_date = datetime.strptime(override_date, '%Y%m%d').replace(tzinfo=timezone.utc)
        print(f"[TEST MODE] Using override date: {override_date}")
    else:
        current_date = datetime.now(timezone.utc)

    current_date_str = current_date.strftime('%Y%m%d')
    current_date_display = current_date.strftime('%Y-%m-%d')

    all_links = get_all_compose_links()
    if not all_links:
        send_slack_notification([{
            "type": "section",
            "text": {"type": "mrkdwn", "text": ":x: *CRITICAL:* Could not fetch the Fedora IoT compose index."}
        }])
        sys.exit(1)

    versions = detect_active_versions(all_links)
    if not versions:
        print("No active versions detected. Exiting.")
        sys.exit(1)

    reports = []
    has_failure = False
    for version in versions:
        report = inspect_version(version, all_links, current_date_str)
        reports.append(report)
        if report["status"] in ("DOOMED", "FINISHED_INCOMPLETE", "MISSING"):
            has_failure = True

    print(f"\n{'='*20} Summary {'='*20}")
    for r in reports:
        icon = {"FINISHED": "OK", "STARTED": "IN PROGRESS"}.get(r["status"], "ISSUE")
        print(f"  [{icon}] Fedora IoT {r['version']}: {r['status']}")

    send_slack_notification(format_slack_blocks(current_date_display, reports))
    save_daily_report(current_date_str, reports)

    if has_failure:
        print("\nInspection finished with one or more issues.")
    else:
        print("\nInspection finished successfully for all versions.")


if __name__ == "__main__":
    main()
