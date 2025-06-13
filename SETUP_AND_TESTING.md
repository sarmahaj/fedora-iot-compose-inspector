# Project Setup and Local Testing Guide

This document provides all the necessary steps for a developer to set up the repository for the first time and to test the script locally.

## 1. One-Time Project Setup

To make the automation work, you must generate three secret keys and add them to your GitHub repository's settings.

### Step 1.1: Generate the Secrets

**1. Google Gemini API Key**
This key is used for the AI analysis of log files.

* **How to get it:** Since you already have a Google Cloud Console login, follow these precise steps:
    1.  Go to the [Google Cloud Console](https://console.cloud.google.com/). 
        Create a Project (eg. fedora-iot-inspector). At the top of the page, select an existing project or create a new one.
    2.  **Enable the API:**
        * Using the top search bar, search for **"Generative Language API"**.
        * Click on it from the search results and then click the **"Enable"** button. This authorizes your project to use the API.
    3.  **Create the API Key:**
        * Using the navigation menu (☰), go to **`APIs & Services > Credentials`**.
        * Click **`+ CREATE CREDENTIALS`** at the top and select **`API key`**.
        * A new key will be generated. Copy it and save it somewhere temporarily.
    4.  **Secure the API Key (Important):**
        * In the list of keys, find your new key and click on its name to edit it.
        * Under **Application restrictions**, select `None`. This is necessary for the script to use the key from the GitHub Actions server.
        * Under **API restrictions**, select `Restrict key`. In the dropdown that appears, find and check the box for **`Generative Language API`**.
        * Click **`SAVE`**.

**2. GitHub Personal Access Token**
This token is used to search for related issues on GitHub to provide more context to the AI.

* **How to get it:** Follow the guide to create a "classic" Personal Access Token.
* **Guide:** [Creating a personal access token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#creating-a-personal-access-token-classic)
  * **Scope/Permission needed:** You only need to grant the `public_repo` scope.
  * **Note:** Copy the token immediately after generation. You will not see it again.

**3. Slack Incoming Webhook URL**
This URL is used to post the final summary report to a Slack channel.
Note: Since a slack channel is already created, all the developers need not
create a new webhook. The same can be copied to your settings.
SLACK_WEBHOOK_URL=""

(still mentioning the step if needed to create from scratch)
* **How to get it:** Follow the guide to create a new Incoming Webhook.
* **Guide:** [Sending messages using Incoming Webhooks](https://api.slack.com/messaging/webhooks)
  1. Create a minimal Slack "App".
  2. Activate "Incoming Webhooks" for the app.
  3. Add a new webhook to your desired workspace and channel. (This may require approval from your Slack workspace admin).
  4. Copy the Webhook URL (it starts with `https://hooks.slack.com/...`).

### Step 1.2: Add Secrets to GitHub

1. In your GitHub repository, go to **`Settings`**.
2. In the left sidebar, navigate to **`Security` > `Secrets and variables` > `Actions`**.
3. Ensure you are on the **`Secrets`** tab.
4. Click **`New repository secret`** for each of the keys you generated. **The names must be an exact match**:
   * `GEMINI_API_KEY`: The key you got from Google Cloud Console.
   * `MY_GITHUB_TOKEN`: The `ghp_...` token you got from GitHub.
   * `SLACK_WEBHOOK_URL`: The webhook URL you got from Slack.

---

## 2. Local Testing

### Step 2.1: Create a Virtual Environment

It is a best practice to use a virtual environment to manage project-specific dependencies.

```bash
# From your project's root directory
python -m venv .venv
```

### Step 2.2: Activate the Virtual Environment

```bash
source .venv/bin/activate
```
Your terminal prompt should now be prefixed with (.venv).

### Step 2.3: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 2.4: Create a Local .env File for Secrets
The script needs your secret keys to run locally. In the root directory of your project, create a new file named .env. 
Add your secrets to this file using the format VARIABLE_NAME="value".
This file is for local development only. Do not commit it.

GEMINI_API_KEY="paste_your_google_api_key_here"
MY_GITHUB_TOKEN="ghp_YourGitHubTokenGoesHere"
SLACK_WEBHOOK_URL="[https://hooks.slack.com/services/your/webhook/url/here](https://hooks.slack.com/services/your/webhook/url/here)"

### Step 2.5: Run the Script
You are now ready to run the script locally.

```bash
python check_fedora_iot.py
```
