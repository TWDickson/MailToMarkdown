# 📬 MailToMarkdown

**Export your emails as Markdown files – attachments included.**  
Perfect for archiving, searching, or importing into your [Obsidian](https://obsidian.md/) vault.

---

## 💭 Motivation

At the school where I recently graduated, every student received their own email account. Over the years, I received many useful, funny, and personal messages that I didn’t want to lose.

As a heavy Obsidian user, I wanted to keep those emails in a well-structured, searchable Markdown format – as part of my personal knowledge archive.

Instead of manually exporting every message, I built this tool (with the help of AI) to make the job easier and share it with others who might want the same – **so you don’t have to write 20 prompts just to get a usable script** 😉

---

## 🛠 Features

- Interactive command-line input (server, port, folder, etc.)
- Connects via **IMAP over SSL**
- Parses and saves each email as `.md` with metadata
- Saves attachments and links them in the Markdown
- Displays progress with a nice terminal bar
- Outputs clean, portable files for backup or import
- **Smart quote removal** - automatically removes quoted reply history to reduce LLM token usage
- **Contentless forward filtering** - skips forwarded emails with no original content

---

## ▶️ How to Use

### 🧱 1. Installation

Clone the repo and install dependencies:

```bash
git clone https://github.com/yourusername/mailtomarkdown.git
cd mailtomarkdown
pip install -r requirements.txt
```

#### 📦 requirements.txt


```
markdownify
html2text
tqdm
keyring
```


#### 🚀 2. Run the script

```
python export_mails.py
```


#### You'll be asked for:

- Your email address

- Your password (secure input)

- IMAP server (e.g. imap.example.com)

- Port (usually 993)

- Primary folder name (e.g., INBOX, Important/Estate)

- Whether to include related sent emails (replies to emails in your primary folder)

- Output folder (e.g. email_export)

After that, the export starts. All emails are saved as .md files in the output folder, and all attachments go to attachments/.

**💡 New: Smart Sent Email Filtering**
- Optionally include sent emails that are replies to emails in your primary folder
- Only exports relevant sent emails (not your entire Sent folder)
- Perfect for capturing complete email conversations without clutter
- Each email includes a `folder:` field in its metadata showing which folder it came from
- Includes `message_id` and `in_reply_to` fields to track conversation threads

#### 🔐 3. Configuration Caching (Optional)

On first run, you'll be asked if you want to save your configuration. If you choose yes:

- **Non-sensitive settings** (email, server, port, folder) are saved to `mail_config.json`
- **Password** is saved securely to your system's credential manager (Windows Credential Manager, macOS Keychain, or Linux Secret Service)
- On subsequent runs, you can choose to use the saved configuration
- If a saved password is found, it will be used automatically (no need to re-enter)

**Benefits:**
- ✅ Saves time on repeated exports
- ✅ Password stored securely using OS-native credential storage
- ✅ Easy to update or remove saved credentials

**To remove saved credentials:**
- Delete `mail_config.json` for configuration
- Use your OS credential manager to remove the password:
  - **Windows**: Credential Manager → Generic Credentials → MailToMarkdown
  - **macOS**: Keychain Access → search for "MailToMarkdown"
  - **Linux**: Use `secret-tool` or your distribution's credential manager

---

## 🧹 Smart Quote Removal (Built-in)

The export tool now includes **intelligent quote removal** using a hybrid approach that combines separator detection with dynamic content matching:

**How it works:**

- Detects common reply separators ("On [date] [person] wrote:")
- **Dynamically matches content** against parent emails using `in_reply_to` headers
- Processes emails **chronologically** so parent emails are analyzed before replies
- Removes quoted content found via content matching (100+ char matches)
- Skips contentless forwarded emails automatically
- Preserves your original writing and intentional short quotes

**Why it's robust:**

- Uses actual email thread relationships (`message_id` → `in_reply_to`)
- Content matching catches quotes even without separators
- Won't remove your intentional quotes (< 100 chars)
- Handles inline replies and multi-level quote chains

**Configuration:**

During setup, you'll be asked:

```text
🧹 Remove Quoted Replies:
   Remove quoted message history to reduce file size for LLM processing?
   (Recommended: keeps only original content, removes duplicated quotes)
Remove quoted replies? (Y/n):
```

**Results:**

- Typical reduction: 70-90% for reply emails
- Only original content is preserved
- Thread relationships maintained via `message_id` and `in_reply_to` metadata
- No information loss - just removes duplicates

**Technical details:** See [SMART_QUOTE_REMOVAL.md](SMART_QUOTE_REMOVAL.md)

---

#### 🧠 AI-Generated Notice
This tool was written largely with the help of ChatGPT (OpenAI), guided by personal goals and needs. I’m sharing it so that others don’t need to go through the same back-and-forth to get a working solution.

Feel free to fork, improve, or adapt it 💌

📂 Example Markdown Output
markdown
---
from: teacher@school.de
to: calvin@example.com
date: 2024-03-12_10-23-05
subject: Final Exam Results
attachments:
  - [Grades.pdf](attachments/Grades.pdf)
---

Hi Calvin,

Attached are your results. Congrats!

Best,  
Your teacher


---

### 🖤 License
MIT – use it however you want.

### ☕ Author
Made with love and memory-preserving intention by Calvin-Nevaro Erfmann

---

