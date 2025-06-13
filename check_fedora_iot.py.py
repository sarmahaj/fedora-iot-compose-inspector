import requests
from bs4 import BeautifulSoup
import sys
import re

# --- Configuration ---
COMPOSE_BASE_URL = "https://kojipkgs.fedoraproject.org/compose/iot/"

def get_latest_compose_url():
    """Finds the URL for the most recent compose directory."""
    print(f"🔍 Searching for the latest compose directory in {COMPOSE_BASE_URL}...")
    try:
        response = requests.get(COMPOSE_BASE_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        compose_links = [link.get('href') for link in soup.find_all('a') if link.get('href', '').startswith('Fedora-IoT-')]
        
        if not compose_links:
            print("❌ No compose directories found.")
            return None
            
        latest_compose_dir = sorted(compose_links)[-1]
        print(f"✅ Found latest compose: {latest_compose_dir}")
        return f"{COMPOSE_BASE_URL}{latest_compose_dir}"

    except requests.exceptions.RequestException as e:
        print(f"Error finding latest compose: {e}")
        return None

def check_compose_status(compose_url):
    """Dynamically finds and checks the status of all composes at the given URL."""
    print(f"\nInspecting composes at: {compose_url}\n")
    has_failures = False
    
    try:
        response = requests.get(compose_url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        compose_dirs = [link.get('href') for link in soup.find_all('a', href=re.compile(r'Fedora-IoT-.*-ostree-.*\/$'))]

        if not compose_dirs:
            print("Could not find any compose sub-directories to check.")
            return True

        for version_dir in compose_dirs:
            status_url = f"{compose_url}{version_dir}STATUS"
            version_name = version_dir.replace('/', '')
            
            print(f"--- Checking {version_name} ---")
            try:
                status_response = requests.get(status_url)
                if status_response.status_code == 200:
                    status = status_response.text.strip()
                    print(f"  Status: {status}")

                    if "FINISHED_INCOMPLETE" in status:
                        logs_url = f"{compose_url}{version_dir}logs/pungi.global.log"
                        print(f"  🚨 INVESTIGATION NEEDED. Global log: {logs_url}")
                        has_failures = True
                    elif "DOOMED" in status:
                        print("  🔥 DOOMED. Check osbuild for failures.")
                        has_failures = True
                    elif "FINISHED" in status:
                        print("  ✅ Compose completed successfully.")
                else:
                    print(f"  Could not read STATUS file (HTTP {status_response.status_code})")
            
            except requests.exceptions.RequestException as e:
                print(f"  Error checking status: {e}")
                has_failures = True
            print("-" * 30)
            
    except requests.exceptions.RequestException as e:
        print(f"Failed to access compose directory: {e}")
        return True
        
    return has_failures

if __name__ == "__main__":
    print("🚀 Starting Fedora IoT Compose Inspection 🚀")
    latest_compose_url = get_latest_compose_url()
    
    if latest_compose_url:
        any_failures = check_compose_status(latest_compose_url)
        
        if any_failures:
            print("\n‼️ Inspection finished with failures.")
            sys.exit(1)
        else:
            print("\n🎉 Inspection finished successfully.")
    else:
        print("\nCould not perform inspection. No compose URL found.")
        sys.exit(1)