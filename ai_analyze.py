import os
import sys
import google.generativeai as genai

# Get the API key from GitHub Secrets
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    sys.exit("Error: GEMINI_API_KEY secret not found.")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# The user's question is passed as a command-line argument
user_question = sys.argv[1] if len(sys.argv) > 1 else "What went wrong? Please summarize the key points."

# Read the log file that the workflow will provide
try:
    with open('log.txt', 'r') as f:
        log_content = f.read()
except FileNotFoundError:
    sys.exit("Error: log.txt not found.")

# Create the prompt for the AI
prompt = f"""
You are an expert DevOps engineer specializing in Fedora build systems.
Your task is to analyze the following log file from a Fedora IoT compose check and answer the user's question based on the log's content.

**User's Question:** {user_question}

**Log File Content:**
---
{log_content}
---

**Your Analysis:**
Provide a clear, concise answer in markdown format. If you find a specific error message, quote it in a code block.
Suggest the most likely root cause and recommend the next debugging steps in a numbered list.
"""

# Call the AI and print its response
response = model.generate_content(prompt)
print(response.text)