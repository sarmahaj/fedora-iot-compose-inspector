import requests
from bs4 import BeautifulSoup
import sys
import re
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
import google.generativeai as genai
from github import Github
import time
import json

# --- Load Environment Variables ---
# This loads variables from a local .env file for development.
# In GitHub Actions, these are set by the workflow.
load_dotenv()

# --- Configurations ---
COMPOSE_BASE_URL = "https://kojipkgs.fedoraproject.org/compose/iot/"
VERSIONS_TO_CHECK = ["43", "42", "41"]
REPOS_TO_SEARCH = ["osbuild/osbuild-composer", "osbuild/images"]
RETRY_COUNT = 3
RETRY_DELAY_SECONDS = 60
RUN_URL = f"https://github.com/{os.getenv('GITHUB_REPOSITORY', 'your/repo')}/actions/runs/{os.getenv('GITHUB_RUN_ID', 'local')}"

# --- API/Client Initialization ---
# Gemini AI Configuration
try:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
    else:
        raise ValueError("GEMINI_API_KEY not found in environment.")
except Exception as e:
    print(f"⚠️ Could not configure Gemini API: {e}. AI analysis will be disabled.")
    GEMINI_API_KEY = None

# GitHub API Configuration
try:
    GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
    g = Github(GITHUB_TOKEN)
    # A quick check to ensure the token is valid by getting the authenticated user
    _ = g.get_user().login
    print("✅ GitHub client configured successfully with a token.")
except Exception:
    print(f"⚠️ GITHUB_TOKEN not found or invalid. Using unauthenticated GitHub API with stricter rate limits.")
    g = Github()

# Slack Webhook Configuration
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")


# --- Helper Functions ---

def get_url_content(url):
    """Generic function to fetch text content from a URL."""
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        print(f"    -> ERROR: Could not fetch {url}. Reason: {e}")
        return None

def find_koji_task_url_from_osbuild_logs(osbuild_dir_url):
    """Scrapes the osbuild log directory to find the Koji task URL from watch-task logs."""
    print(f"    -> Searching for Koji task URL in: {osbuild_dir_url}")
    dir_content = get_url_content(osbuild_dir_url)
    if not dir_content: return None
    soup = BeautifulSoup(dir_content, 'html.parser')
    watch_task_logs = soup.find_all('a', href=re.compile(r'IoT-\d+-watch-task\.log$'))
    for log_link in watch_task_logs:
        log_file_url = f"{osbuild_dir_url}{log_link['href']}"
        print(f"      -> Reading {log_link['href']}...")
        log_content = get_url_content(log_file_url)
        if log_content:
            koji_task_match = re.search(r'(https://koji\.fedoraproject\.org/koji/taskinfo\?taskID=\d+)', log_content)
            if koji_task_match:
                koji_url = koji_task_match.group(1)
                print(f"      -> Found Koji Task URL: {koji_url}")
                return koji_url
    print("    -> No Koji task URL found in any of the watch-task logs.")
    return None

def get_final_error_from_koji_task(koji_task_url):
    """Navigates to a Koji task URL, finds compose-status.json, and returns its content."""
    print(f"    -> Drilling down into Koji Task for definitive error: {koji_task_url}")
    koji_page_content = get_url_content(koji_task_url)
    if not koji_page_content: return "Could not fetch the Koji task page."
    soup = BeautifulSoup(koji_page_content, 'html.parser')
    json_link = soup.find('a', href=re.compile(r'.*compose-status\.json$'))
    if not json_link: return "Could not find 'compose-status.json' link on the Koji task page."
    json_url = json_link['href']
    if not json_url.startswith('http'):
        base_koji_url = "https://kojipkgs.fedoraproject.org/"
        json_url = base_koji_url + json_url
    print(f"      -> Found definitive error log: {json_url}")
    json_content = get_url_content(json_url)
    if not json_content: return f"Could not fetch content from {json_url}."
    try:
        parsed_json = json.loads(json_content)
        return json.dumps(parsed_json, indent=2)
    except json.JSONDecodeError:
        return json_content

def run_ai_analysis(context, prompt_instructions):
    """Generic function to run AI analysis with a given context and prompt."""
    if not GEMINI_API_KEY: return "AI analysis skipped: API key not configured."
    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"{prompt_instructions}\n\n**Context:**\n---\n{context}\n---"
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"An error occurred during AI analysis: {e}"

def send_slack_notification(summary_message):
    """Formats and sends a summary message to the configured Slack webhook URL."""
    if not SLACK_WEBHOOK_URL:
        print("⚠️ SLACK_WEBHOOK_URL not found. Skipping Slack notification.")
        return
    slack_data = {
        "text": summary_message,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": summary_message}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"For full details, see the <{RUN_URL}|GitHub Actions run log>."}]}
        ]
    }
    print(f"Sending summary to Slack webhook...")
    try:
        response = requests.post(SLACK_WEBHOOK_URL, data=json.dumps(slack_data), headers={'Content-Type': 'application/json'}, timeout=30)
        response.raise_for_status()
        print("✅ Slack notification sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"❌ Error sending Slack notification: {e}")

# --- Main Logic Functions ---

def get_all_compose_links():
    """Fetches the main page and returns all hyperlink elements, with a retry mechanism."""
    for attempt in range(RETRY_COUNT):
        try:
            print(f"Attempting to fetch main compose page (Attempt {attempt + 1}/{RETRY_COUNT})...")
            response = requests.get(COMPOSE_BASE_URL)
            response.raise_for_status()
            print("✅ Page fetched successfully.")
            return BeautifulSoup(response.text, 'html.parser').find_all('a')
        except requests.exceptions.RequestException as e:
            print(f"❌ Attempt {attempt + 1} failed: {e}")
            if attempt < RETRY_COUNT - 1:
                print(f"Retrying in {RETRY_DELAY_SECONDS} seconds...")
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                print("❌ Critical Error: Could not fetch the main compose page after multiple attempts.")
                return None

def find_latest_compose_for_version(version, all_links):
    """Finds the URL for the most recent compose directory for a specific version."""
    print(f"🔍 Searching for the latest compose for Fedora IoT {version}...")
    pattern = re.compile(f"Fedora-IoT-{version}-\d+\.\d+\/")
    version_links = [link.get('href') for link in all_links if pattern.match(link.get('href', ''))]
    if not version_links:
        print(f"  -> No composes found for version {version}.")
        return None
    latest_compose_dir = sorted(version_links)[-1]
    print(f"  -> Found latest: {latest_compose_dir}")
    return f"{COMPOSE_BASE_URL}{latest_compose_dir}"

def inspect_compose_url(compose_url, version_name):
    """Runs the multi-step diagnostic process and returns a summary string and status."""
    status_url = f"{compose_url}STATUS"
    try:
        status_content = get_url_content(status_url)
        if not status_content:
            return f"❌ *{version_name}:* Could not fetch STATUS file.", True
        status = status_content.strip()
        print(f"  -> Status: {status}")

        if status == "STARTED":
            return f"⏳ *{version_name}:* Compose is in progress.", False
        if status == "FINISHED":
            return f"✅ *{version_name}:* Compose finished successfully.", False

        # If we reach here, it's a failure.
        failure_summary = f"🔥 *{version_name}:* Failed with status `{status}`."
        print(f"  🚨 Failure detected for {version_name}. Starting sequential diagnosis...")

        # Step 1: Analyze Pungi Global Log
        print("\n  --- Diagnosis Step 1: Analyzing pungi.global.log ---")
        pungi_log_url = f"{compose_url}logs/global/pungi.global.log"
        pungi_log = get_url_content(pungi_log_url)
        if pungi_log:
            pungi_prompt = "Analyze this `pungi.global.log`. Is the root cause a high-level compose issue (repo, disk space) OR a lower-level image build failure? If it's a build failure, you MUST state that deeper Koji log analysis is needed."
            pungi_analysis = run_ai_analysis(pungi_log[-5000:], pungi_prompt)
            print(pungi_analysis)
            if "deeper analysis" not in pungi_analysis.lower() and "koji" not in pungi_analysis.lower():
                failure_summary += f"\n\n*AI Diagnosis (from pungi.global.log):*\n```{pungi_analysis}```"
                return failure_summary, True

        # Step 2: Drill down into Koji logs if needed
        print("\n  --- Diagnosis Step 2: Finding and Analyzing Koji Task Error JSON ---")
        osbuild_dir_url = f"{compose_url}logs/global/osbuild/"
        koji_task_url = find_koji_task_url_from_osbuild_logs(osbuild_dir_url)
        if koji_task_url:
            definitive_error_json = get_final_error_from_koji_task(koji_task_url)
            print("\n  --- Diagnosis Step 3: Final AI Synthesis ---")
            # THIS IS THE UPDATED, MORE INTELLIGENT PROMPT
            final_prompt = """
            You are an expert Fedora build engineer. Based on this definitive `compose-status.json` from a Koji build task, provide a final diagnosis and a numbered list of recommended actions.

            **Your Analysis Rules:**
            1.  **Prioritize Tracebacks:** A `FileNotFoundError` inside a Python traceback is the most likely root cause. It indicates a missing package dependency in the build root.
            2.  **Identify Missing Executables:** If you see a `FileNotFoundError` for a specific command (e.g., `FileNotFoundError: [Errno 2] No such file or directory: 'usermod'`), you MUST identify the Fedora package that provides that command (e.g., `usermod` is provided by the `shadow-utils` package) and state that this package is missing from the build dependencies.
            3.  **Ignore Noise:** Disregard repetitive, non-fatal errors like "Read-only file system" for `/sys/fs/selinux/`, as they are symptoms of the build environment, not the root cause.
            4.  **Be Directive:** Your recommended actions must be specific and actionable (e.g., "Add `shadow-utils` to the package set for this image build.").
            """
            final_analysis = run_ai_analysis(definitive_error_json, final_prompt)
            failure_summary += f"\n\n*AI Diagnosis (from Koji Task {koji_task_url.split('=')[-1]}):*\n```{final_analysis}```"
        else:
            failure_summary += "\n> _Could not find a Koji task URL to perform deep analysis._"
        return failure_summary, True

    except Exception as e:
        return f"❌ *{version_name}:* An unexpected error occurred: {e}", True

def main():
    """The main execution function."""
    print("🚀 Starting Intelligent Fedora IoT Compose Diagnosis 🚀")
    results_summary = []
    overall_failure = False
    all_links = get_all_compose_links()
    if all_links:
        for version in VERSIONS_TO_CHECK:
            print("-" * 40)
            latest_url = find_latest_compose_for_version(version, all_links)
            if latest_url:
                summary_line, has_failed = inspect_compose_url(latest_url, f"Fedora-IoT-{version}")
                results_summary.append(summary_line)
                if has_failed:
                    overall_failure = True
            else:
                results_summary.append(f"⚪ *Fedora-IoT-{version}:* No compose found to check.")
        print("-" * 40)
        final_message = f"📰 *Fedora IoT Compose Status Summary - {datetime.now().strftime('%Y-%m-%d')}*\n\n" + "\n\n".join(results_summary)
        send_slack_notification(final_message)
        if overall_failure:
            print("\n‼️ Inspection finished with one or more failures.")
            sys.exit(1)
        else:
            print("\n🎉 Inspection finished successfully for all targeted versions.")
    else:
        send_slack_notification("❌ Critical Error: Could not fetch the main Fedora IoT compose page to begin the inspection.")
        sys.exit(1)

if __name__ == "__main__":
    main()

