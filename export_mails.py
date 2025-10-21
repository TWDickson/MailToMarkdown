import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import os
import re
import json
import sys
import signal
from getpass import getpass
from tqdm import tqdm
import keyring
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Event
from pathlib import Path

CONFIG_FILE = "mail_config.json"
KEYRING_SERVICE = "MailToMarkdown"

# Global shutdown event for graceful termination
shutdown_event = Event()

# Global email database for quote detection
email_database = {}
email_database_lock = Lock()


def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    print("\n\n⚠️  Interrupt received! Shutting down gracefully...")
    print("   Please wait for threads to finish...")
    shutdown_event.set()


# Register signal handler
signal.signal(signal.SIGINT, signal_handler)


# 🧼 Clean up filenames
def sanitize_filename(s):
    s = re.sub(r'[\r\n\t]', '', s)
    s = s.strip()
    s = re.sub(r'[\\/*?:"<>|]', "_", s)
    return s


def clean_header_field(text):
    """Clean up email header fields (To, From, CC, BCC) by removing extra whitespace."""
    if not text:
        return ""

    # Replace newlines and tabs with spaces
    text = re.sub(r'[\r\n\t]+', ' ', text)

    # Replace multiple spaces with single space
    text = re.sub(r' +', ' ', text)

    # Strip leading/trailing whitespace
    return text.strip()


def convert_urls_to_markdown(text):
    """Convert plain URLs to markdown link format."""
    if not text:
        return ""

    # First, clean up mailto links in angle brackets
    text = re.sub(r'<mailto:([^>]+)>', r'\1', text)

    # Clean up URLs wrapped in angle brackets like <http://example.com>
    text = re.sub(r'<((?:https?|ftp)://[^>]+)>', r'\1', text)

    # Remove cases where markdown links are wrapped in angle brackets like <[text](url)>
    text = re.sub(r'<(\[[^\]]+\]\([^\)]+\))>', r'\1', text)

    # Pattern to match plain URLs that aren't already in markdown format [text](url)
    # Negative lookbehind: not preceded by ( or [
    # Negative lookahead: not followed by )
    url_pattern = r'(?<!\()(?<!\[)\b((?:https?|ftp)://[^\s\)<>\]]+)(?!\))(?!\])'

    # Replace with markdown link format
    text = re.sub(url_pattern, r'[\1](\1)', text)

    return text


def clean_email_body(text):
    """Clean up email body formatting while preserving paragraph breaks."""
    if not text:
        return ""

    # Split into lines
    lines = text.split('\n')

    # Remove trailing whitespace from each line
    lines = [line.rstrip() for line in lines]

    # Join back together
    text = '\n'.join(lines)

    # Replace 3 or more consecutive newlines with just 2 (one blank line)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Remove blank lines at the very start and end
    text = text.strip()

    # Convert URLs to markdown links
    text = convert_urls_to_markdown(text)

    return text


def detect_reply_separator(body):
    """
    Detect where quoted/replied content starts in an email body.
    Returns the line index where quoting begins, or None if not found.
    """
    if not body:
        return None

    lines = body.split('\n')

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Pattern: "On [date/time] ... wrote:"
        if re.match(r'^On\s+.+?\d+.+?wrote:\s*$', stripped, re.IGNORECASE):
            return i

        # Pattern: Forwarded message divider
        if re.match(r'^[-=]{5,}\s*Forwarded\s+message\s*[-=]{5,}', stripped, re.IGNORECASE):
            return i

        if re.match(r'^Begin\s+forwarded\s+message:', stripped, re.IGNORECASE):
            return i

        # Pattern: Multiple consecutive lines starting with >
        # (need at least 3 to avoid false positives with blockquotes)
        if stripped.startswith('>'):
            # Check if next few lines also start with >
            consecutive_quotes = 1
            for j in range(i + 1, min(i + 4, len(lines))):
                if lines[j].strip().startswith('>'):
                    consecutive_quotes += 1

            if consecutive_quotes >= 3:
                return i

    return None


def is_contentless_forward(metadata, body):
    """Check if this is a forward with no original content."""
    subject = metadata.get('subject', '').strip()

    # Check if it's a forward
    is_forward = (subject.startswith('Fwd_') or
                  subject.startswith('FW:') or
                  subject.startswith('Fwd:'))

    if not is_forward:
        return False

    # Check if there's any content before the reply separator
    separator_idx = detect_reply_separator(body)

    if separator_idx is None:
        # No separator found, check body length
        return len(body.strip()) < 10

    # Check content before separator
    lines = body.split('\n')
    original_content = '\n'.join(lines[:separator_idx]).strip()

    return len(original_content) < 10


def load_config():
    """Load saved configuration from file."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Could not load config: {e}")
    return {}


def save_config(config):
    """Save configuration to file (excluding password)."""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        print(f"💾 Configuration saved to {CONFIG_FILE}")
    except Exception as e:
        print(f"⚠️ Could not save config: {e}")


def get_saved_password(email_addr):
    """Retrieve password from system keyring."""
    try:
        return keyring.get_password(KEYRING_SERVICE, email_addr)
    except Exception as e:
        print(f"⚠️ Could not retrieve password: {e}")
        return None


def save_password(email_addr, password):
    """Save password to system keyring."""
    try:
        keyring.set_password(KEYRING_SERVICE, email_addr, password)
        print("🔐 Password saved securely to system keyring")
    except Exception as e:
        print(f"⚠️ Could not save password: {e}")


def get_configuration():
    """Get configuration from user input or saved config."""
    config = load_config()
    use_saved = False

    if config:
        print("📋 Found saved configuration:")
        print(f"   Email: {config.get('email', 'N/A')}")
        print(f"   IMAP Server: {config.get('imap_server', 'N/A')}")
        print(f"   IMAP Port: {config.get('imap_port', 'N/A')}")
        folders = config.get('imap_folders', ['INBOX'])
        print(f"   IMAP Folders: {', '.join(folders)}")
        include_sent = config.get('include_related_sent', False)
        print(f"   Include Related Sent: {'Yes' if include_sent else 'No'}")
        remove_quotes = config.get('remove_quotes', True)
        print(f"   Remove Quoted Replies: {'Yes' if remove_quotes else 'No'}")
        print(f"   Output Dir: {config.get('output_dir', 'N/A')}")
        print()
        sys.stdout.flush()
        use_saved_input = input("Use saved configuration? (Y/n): ").strip().lower()
        use_saved = use_saved_input not in ['n', 'no']
        print()

    if use_saved:
        email_addr = config.get('email', '')
        imap_server = config.get('imap_server', '')
        imap_port = config.get('imap_port', 993)
        imap_folders = config.get('imap_folders', ['INBOX'])
        include_related_sent = config.get('include_related_sent', False)
        remove_quotes = config.get('remove_quotes', True)
        output_dir = config.get('output_dir', 'export')

        # Try to get saved password
        password = get_saved_password(email_addr)
        if password:
            print("🔑 Using saved password")
            print()
        else:
            password = getpass("Password: ")
            save_pass = input("Save password securely? (y/n): ").strip().lower()
            if save_pass in ['y', 'yes']:
                save_password(email_addr, password)
            print()
    else:
        email_addr = input("Email address: ").strip()
        imap_server = input("IMAP server (e.g. imap.example.com): ").strip()
        imap_port = int(input("IMAP port (e.g. 993): ").strip())

        print("\n📁 Folder Selection:")
        print("   Primary folder to export (e.g., INBOX, Important/Estate)")
        imap_folders = [input("Primary IMAP folder: ").strip() or "INBOX"]

        print("\n📤 Include Related Sent Emails:")
        print("   Export sent emails that are replies to emails in the primary folder?")
        include_related_sent_input = input("Include related sent emails? (y/n): ").strip().lower()
        include_related_sent = include_related_sent_input in ['y', 'yes']

        print("\n🧹 Remove Quoted Replies:")
        print("   Remove quoted message history to reduce file size for LLM processing?")
        print("   (Recommended: keeps only original content, removes duplicated quotes)")
        remove_quotes_input = input("Remove quoted replies? (Y/n): ").strip().lower()
        remove_quotes = remove_quotes_input not in ['n', 'no']

        output_dir = input("\nOutput directory (e.g. export): ").strip() or "export"
        password = getpass("Password: ")

        # Ask if user wants to save configuration
        save_config_input = input("\nSave this configuration? (y/n): ").strip().lower()
        if save_config_input in ['y', 'yes']:
            new_config = {
                'email': email_addr,
                'imap_server': imap_server,
                'imap_port': imap_port,
                'imap_folders': imap_folders,
                'include_related_sent': include_related_sent,
                'remove_quotes': remove_quotes,
                'output_dir': output_dir
            }
            save_config(new_config)

            save_pass = input("Save password securely? (y/n): ").strip().lower()
            if save_pass in ['y', 'yes']:
                save_password(email_addr, password)

    return {
        'email': email_addr,
        'password': password,
        'imap_server': imap_server,
        'imap_port': imap_port,
        'imap_folders': imap_folders,
        'include_related_sent': include_related_sent,
        'remove_quotes': remove_quotes,
        'output_dir': output_dir
    }


def parse_email_message(msg, folder_name):
    """Parse an email message and extract metadata."""
    subject = decode_header(msg["Subject"] or "no_subject")[0][0]
    if isinstance(subject, bytes):
        subject = subject.decode(errors="ignore")
    subject = sanitize_filename(subject or "no_subject")

    from_ = msg.get("From", "unknown")
    to = msg.get("To", "unknown")
    cc = clean_header_field(msg.get("Cc", ""))
    bcc = clean_header_field(msg.get("Bcc", ""))
    message_id = msg.get("Message-ID", "")
    in_reply_to = msg.get("In-Reply-To", "")

    date = msg.get("Date", "")
    try:
        date_obj = parsedate_to_datetime(date)
        date_fmt_filename = date_obj.strftime("%Y-%m-%d_%H-%M-%S")
        date_fmt_iso = date_obj.strftime("%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        date_fmt_filename = "unknown_date"
        date_fmt_iso = "unknown"

    return {
        'subject': subject,
        'from': from_,
        'to': to,
        'cc': cc,
        'bcc': bcc,
        'message_id': message_id,
        'in_reply_to': in_reply_to,
        'date_fmt_filename': date_fmt_filename,
        'date_fmt_iso': date_fmt_iso,
        'folder': folder_name
    }


def get_email_filename(metadata, imap_uid=None):
    """Generate consistent filename for email (without extension)."""
    # Include IMAP UID in filename for tracking
    uid_part = f"_{imap_uid}" if imap_uid else ""
    return f"{metadata['date_fmt_filename']}{uid_part}_{metadata['subject'][:50]}"


def get_folder_subdir(folder_name):
    """Convert folder name to safe subdirectory name."""
    # Replace / with _ for folder hierarchy (e.g., "Important/Estate" -> "Important_Estate")
    safe_name = folder_name.replace('/', '_').replace('\\', '_')
    return sanitize_filename(safe_name)


def save_raw_email(raw_email_bytes, metadata, raw_emails_dir, imap_uid=None):
    """Save raw email as .eml file in folder-specific subdirectory."""
    # Create folder-specific subdirectory
    folder_subdir = get_folder_subdir(metadata['folder'])
    folder_path = os.path.join(raw_emails_dir, folder_subdir)
    os.makedirs(folder_path, exist_ok=True)

    filename_base = get_email_filename(metadata, imap_uid)
    filepath_eml = os.path.join(folder_path, sanitize_filename(filename_base) + ".eml")

    with open(filepath_eml, "wb") as f:
        f.write(raw_email_bytes)

    return filepath_eml


def get_local_email_uids(raw_emails_dir, folder_name):
    """Get set of IMAP UIDs we already have locally for a folder."""
    folder_subdir = get_folder_subdir(folder_name)
    folder_path = os.path.join(raw_emails_dir, folder_subdir)

    if not os.path.exists(folder_path):
        return set()

    uids = set()
    for filename in os.listdir(folder_path):
        if filename.endswith('.eml'):
            # Extract UID from filename (format: DATE_UID_SUBJECT.eml)
            # Example: 2025-10-06_12-19-17_12345_Re_ a change.eml
            parts = filename.split('_')
            if len(parts) >= 5:  # DATE_TIME_UID_...
                try:
                    # The UID is after date and time (position 3)
                    uid = parts[3]
                    if uid.isdigit():
                        uids.add(uid)
                except (IndexError, ValueError):
                    pass

    return uids


def remove_quoted_reply_chain(body_text):
    """
    Remove quoted reply chain at the end of emails.

    Strategy:
    1. Look for reply separator (e.g., "On [date]... wrote:")
    2. Remove everything from the separator onward (it's the reply chain)
    3. Don't touch inline quotes that appear BEFORE the separator
    """
    if not body_text:
        return body_text

    lines = body_text.split('\n')

    # Find the separator line (e.g., "On Mon, Oct 6, 2025, at 10:55, Brad Fackoury wrote:")
    separator_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Pattern: "On [date/time] ... wrote:"
        if re.match(r'^On\s+.+?\d+.+?wrote:\s*$', stripped, re.IGNORECASE):
            separator_idx = i
            break
        # Pattern: Forwarded message divider
        if re.match(r'^[-=]{5,}\s*Forwarded\s+message\s*[-=]{5,}', stripped, re.IGNORECASE):
            separator_idx = i
            break
        if re.match(r'^Begin\s+forwarded\s+message:', stripped, re.IGNORECASE):
            separator_idx = i
            break

    if separator_idx is None:
        # No separator found, don't remove anything
        # Inline quotes are fine - they provide context
        return body_text

    # Found a separator - remove everything from that point onward
    content_lines = lines[:separator_idx]

    # Remove trailing empty lines
    while content_lines and not content_lines[-1].strip():
        content_lines.pop()

    # Remove common signature line right before separator (like "- Taylor")
    if content_lines:
        last_line = content_lines[-1].strip()
        if (last_line.startswith('-') and len(last_line) < 30 and ' ' in last_line):
            # Looks like "- Name"
            content_lines.pop()
            # Remove trailing empty lines again
            while content_lines and not content_lines[-1].strip():
                content_lines.pop()

    return '\n'.join(content_lines)
def extract_email_body_and_attachments(msg, attachments_dir, remove_quoted_lines=True):
    """Extract email body and save attachments."""
    body = ""
    attachment_paths = []
    attachment_count = 0

    for part in msg.walk():
        content_type = part.get_content_type()
        filename = part.get_filename()

        if filename:
            filename = sanitize_filename(filename)
            filepath = os.path.join(attachments_dir, filename)
            try:
                # Special handling for message/rfc822 (attached emails)
                if content_type == "message/rfc822":
                    # For attached emails, get the raw payload without decoding
                    payload = part.get_payload(i=0)
                    if payload:
                        # Convert the message object to bytes
                        payload_bytes = payload.as_bytes()
                        with open(filepath, "wb") as f:
                            f.write(payload_bytes)
                        attachment_paths.append(f"[{filename}](attachments/{filename})")
                        attachment_count += 1
                    else:
                        print(f"⚠️  Skipped .eml attachment with no content: {filename}")
                else:
                    # Normal attachment handling
                    payload = part.get_payload(decode=True)
                    if payload is not None:  # Check if payload is valid
                        with open(filepath, "wb") as f:
                            f.write(payload)
                        attachment_paths.append(f"[{filename}](attachments/{filename})")
                        attachment_count += 1
                    else:
                        print(f"⚠️  Skipped attachment with no content: {filename}")
            except Exception as e:
                print(f"⚠️  Failed to save attachment: {filename} → {e}")
        elif content_type == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                raw_body = payload.decode(errors="ignore")
                # Optionally remove quoted reply chain from raw body
                if remove_quoted_lines:
                    raw_body = remove_quoted_reply_chain(raw_body)
                body += raw_body

    return body, attachment_paths, attachment_count


def normalize_text_for_matching(text):
    """Normalize text for content matching by removing extra whitespace."""
    if not text:
        return ""
    # Collapse whitespace and normalize
    text = re.sub(r'\s+', ' ', text)
    return text.strip().lower()


def find_longest_common_substring(text1, text2, min_length=100):
    """
    Find the longest common substring between two texts.
    Returns (start_in_text1, length) or None if no match >= min_length.
    """
    from difflib import SequenceMatcher

    matcher = SequenceMatcher(None, text1, text2)
    match = matcher.find_longest_match(0, len(text1), 0, len(text2))

    if match.size >= min_length:
        return (match.a, match.size)
    return None


def remove_reply_separators(body, metadata):
    """
    Remove content after reply separators like "On Mon, Oct 6... wrote:".
    This is applied AFTER removing quoted lines (>) from the raw body.
    """
    if not body:
        return body

    original_body = body

    # Try separator detection
    separator_idx = detect_reply_separator(body)

    if separator_idx is not None:
        lines = body.split('\n')
        # Take everything before the separator
        content_before_separator = lines[:separator_idx]

        # Remove trailing empty lines
        while content_before_separator and not content_before_separator[-1].strip():
            content_before_separator.pop()

        # Remove common email signatures that appear right before the separator
        # Look for short lines (like names) at the end
        if content_before_separator:
            last_line = content_before_separator[-1].strip()
            # If last line is very short (likely a signature like "Brad", "Thanks", etc.)
            # AND there's more content before it, remove it
            if len(last_line) < 20 and len(last_line) > 0 and len(content_before_separator) > 1:
                # Check if it looks like a signature (just a name or short closing)
                # No sentence-ending punctuation or question marks
                if not any(marker in last_line for marker in ['?', '.', '!']) or last_line.endswith(','):
                    content_before_separator.pop()

        # Remove any remaining trailing empty lines
        while content_before_separator and not content_before_separator[-1].strip():
            content_before_separator.pop()

        body = '\n'.join(content_before_separator).strip()

    # If we removed too much (less than 10 chars), keep original
    if len(body.strip()) < 10 and len(original_body.strip()) > 10:
        return original_body

    return body


def create_markdown_from_raw_email(eml_filepath, attachments_dir, remove_quotes=True):
    """
    Create markdown content from a raw .eml file.
    This allows re-processing without re-downloading from IMAP.
    """
    # Load the raw email
    with open(eml_filepath, 'rb') as f:
        raw_email = f.read()

    msg = email.message_from_bytes(raw_email)

    # Extract folder from parent directory name
    # Path structure: raw_emails/Important_Estate/email.eml
    eml_path = Path(eml_filepath)
    folder_subdir = eml_path.parent.name

    # Convert back from safe name to original folder name
    if folder_subdir == "raw_emails":
        folder_name = "INBOX"  # Default if directly in raw_emails
    else:
        # Convert "Important_Estate" back to "Important/Estate"
        folder_name = folder_subdir.replace('_', '/')

    metadata = parse_email_message(msg, folder_name)

    # Extract body with optional quote removal (done at raw text level)
    body, attachment_paths, _ = extract_email_body_and_attachments(
        msg, attachments_dir, remove_quoted_lines=remove_quotes
    )

    # Build markdown
    markdown = f"---\nfrom: {metadata['from']}\nto: {metadata['to']}\n"
    if metadata['cc']:
        markdown += f"cc: {metadata['cc']}\n"
    if metadata['bcc']:
        markdown += f"bcc: {metadata['bcc']}\n"
    markdown += f"date: {metadata['date_fmt_iso']}\nsubject: {metadata['subject']}\nfolder: {metadata['folder']}\n"
    if metadata['message_id']:
        markdown += f"message_id: {metadata['message_id']}\n"
    if metadata['in_reply_to']:
        markdown += f"in_reply_to: {metadata['in_reply_to']}\n"
    markdown += "attachments:\n"

    if not attachment_paths:
        markdown += "  - None\n"
    else:
        for link in attachment_paths:
            markdown += f"  - {link}\n"
    markdown += "---\n\n"

    # Clean the body (remove extra whitespace, convert URLs, etc.)
    cleaned_body = clean_email_body(body)

    markdown += cleaned_body

    return markdown, metadata, body


def download_emails_from_imap(mail, imap_folders, raw_emails_dir, include_sent_folder=False):
    """
    Sync emails from IMAP server - only download new/changed emails.
    Uses IMAP UIDs to track what we already have.
    Returns list of downloaded email metadata.
    """
    downloaded_emails = []

    print("\n📥 Syncing emails from IMAP...\n")

    # First, process primary folders and collect Message-IDs
    # Load existing Message-IDs from already downloaded emails
    primary_message_ids = set()

    # Scan existing emails in primary folders to get their Message-IDs
    for folder_name in imap_folders:
        folder_subdir = get_folder_subdir(folder_name)
        folder_path = os.path.join(raw_emails_dir, folder_subdir)

        if os.path.exists(folder_path):
            for filename in os.listdir(folder_path):
                if filename.endswith('.eml'):
                    try:
                        eml_path = os.path.join(folder_path, filename)
                        with open(eml_path, 'rb') as f:
                            msg = email.message_from_bytes(f.read())
                            message_id = msg.get("Message-ID", "")
                            if message_id:
                                primary_message_ids.add(message_id)
                    except Exception:
                        pass  # Skip problematic files

    print(f"   📋 Loaded {len(primary_message_ids)} Message-IDs from existing primary emails\n")

    for folder_name in imap_folders:
        print(f"📂 Syncing folder: {folder_name}")

        # Select folder in read-only mode
        try:
            status, _ = mail.select(folder_name, readonly=True)
            if status != 'OK':
                print(f"   ⚠️  Failed to select folder '{folder_name}'. Skipping...")
                continue
        except Exception as e:
            print(f"   ⚠️  Error accessing folder '{folder_name}': {e}. Skipping...")
            continue

        # Get all UIDs from server
        try:
            status, messages = mail.uid('search', None, "ALL")
            if status != 'OK':
                print(f"   ⚠️  Failed to search folder '{folder_name}'. Skipping...")
                continue
            server_uids = set(messages[0].split())
        except Exception as e:
            print(f"   ⚠️  Error searching folder '{folder_name}': {e}. Skipping...")
            continue

        # Get UIDs we already have locally
        local_uids = get_local_email_uids(raw_emails_dir, folder_name)

        # Convert to comparable format (bytes to strings)
        server_uids_str = {uid.decode() if isinstance(uid, bytes) else str(uid) for uid in server_uids}

        # Find UIDs we need to download
        uids_to_download = server_uids_str - local_uids

        print(f"   📬 Server has {len(server_uids_str)} emails")
        print(f"   💾 Local has {len(local_uids)} emails")
        print(f"   📥 Need to download {len(uids_to_download)} new emails")

        if len(uids_to_download) == 0:
            print(f"   ✅ '{folder_name}' is up to date\n")
            continue

        # Download new emails
        folder_downloaded = 0
        for uid in tqdm(uids_to_download, desc=f"  Downloading from {folder_name}", unit="email"):
            if shutdown_event.is_set():
                print("\n   ⚠️  Cancelled by user. Stopping download...")
                break

            try:
                # Fetch by UID
                res, msg_data = mail.uid('fetch', uid, "(RFC822)")
                if res == 'OK' and msg_data and msg_data[0]:
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)
                    metadata = parse_email_message(msg, folder_name)

                    # Collect Message-IDs from primary folders for later Sent filtering
                    message_id = metadata.get('message_id', '')
                    if message_id:
                        primary_message_ids.add(message_id)

                    # Save raw email with UID in filename
                    save_raw_email(raw_email, metadata, raw_emails_dir, imap_uid=uid)
                    downloaded_emails.append(metadata)
                    folder_downloaded += 1
            except Exception as e:
                print(f"\n⚠️  Failed to download email UID {uid}: {e}")

        print(f"   ✅ Downloaded {folder_downloaded} new emails from '{folder_name}'\n")

    # Now process Sent folder if requested, but only related emails
    if include_sent_folder and primary_message_ids:
        print(f"📤 Syncing related emails from Sent folder...")
        print(f"   Looking for replies to {len(primary_message_ids)} primary emails")

        try:
            status, _ = mail.select("Sent", readonly=True)
            if status == 'OK':
                # Get all UIDs from Sent folder
                status, messages = mail.uid('search', None, "ALL")
                if status == 'OK':
                    server_uids = set(messages[0].split())
                    local_uids = get_local_email_uids(raw_emails_dir, "Sent")
                    server_uids_str = {uid.decode() if isinstance(uid, bytes) else str(uid) for uid in server_uids}
                    uids_to_check = server_uids_str - local_uids

                    print(f"   📬 Sent folder has {len(server_uids_str)} emails")
                    print(f"   💾 Local has {len(local_uids)} sent emails")
                    print(f"   🔍 Checking {len(uids_to_check)} new sent emails for relationships")

                    # First pass: fetch only headers to check relationships (much faster)
                    related_uids = []
                    for uid in tqdm(uids_to_check, desc="  Scanning headers", unit="email"):
                        if shutdown_event.is_set():
                            print("\n   ⚠️  Cancelled by user. Stopping scan...")
                            break

                        try:
                            # Fetch only headers (much faster than full email)
                            res, msg_data = mail.uid('fetch', uid, "(BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO REFERENCES)])")
                            if res == 'OK' and msg_data and msg_data[0]:
                                header_data = msg_data[0][1]
                                if isinstance(header_data, bytes):
                                    header_text = header_data.decode(errors='ignore')

                                    # Extract In-Reply-To and References
                                    in_reply_to_match = re.search(r'In-Reply-To:\s*(.+?)(?:\r?\n(?!\s)|$)', header_text, re.DOTALL | re.IGNORECASE)
                                    references_match = re.search(r'References:\s*(.+?)(?:\r?\n(?!\s)|$)', header_text, re.DOTALL | re.IGNORECASE)

                                    # Extract all referenced Message-IDs
                                    referenced_ids = set()
                                    if in_reply_to_match:
                                        ref_ids = re.findall(r'<[^>]+>', in_reply_to_match.group(1))
                                        referenced_ids.update(ref_ids)
                                    if references_match:
                                        ref_ids = re.findall(r'<[^>]+>', references_match.group(1))
                                        referenced_ids.update(ref_ids)

                                    # Check if any referenced ID matches our primary emails
                                    if referenced_ids & primary_message_ids:
                                        related_uids.append(uid)

                        except Exception as e:
                            print(f"\n⚠️  Failed to check headers for UID {uid}: {e}")

                    print(f"   📥 Found {len(related_uids)} related sent emails")

                    # Second pass: download only the related emails
                    related_count = 0
                    for uid in tqdm(related_uids, desc="  Downloading related", unit="email"):
                        if shutdown_event.is_set():
                            print("\n   ⚠️  Cancelled by user. Stopping download...")
                            break

                        try:
                            res, msg_data = mail.uid('fetch', uid, "(RFC822)")
                            if res == 'OK' and msg_data and msg_data[0]:
                                raw_email = msg_data[0][1]
                                msg = email.message_from_bytes(raw_email)
                                metadata = parse_email_message(msg, "Sent")

                                save_raw_email(raw_email, metadata, raw_emails_dir, imap_uid=uid)
                                downloaded_emails.append(metadata)
                                related_count += 1

                        except Exception as e:
                            print(f"\n⚠️  Failed to download sent email UID {uid}: {e}")

                    print(f"   ✅ Downloaded {related_count} related sent emails\n")
                else:
                    print(f"   ⚠️  Failed to search Sent folder. Skipping...")
            else:
                print(f"   ⚠️  Failed to select Sent folder. Skipping...")
        except Exception as e:
            print(f"   ⚠️  Error accessing Sent folder: {e}. Skipping...")

    elif include_sent_folder and not primary_message_ids:
        print("   ℹ️  No primary emails found yet, skipping Sent folder sync\n")

    return downloaded_emails


def regenerate_all_markdown(raw_emails_dir, output_dir, attachments_dir, remove_quotes=True):
    """
    Regenerate all markdown files from raw .eml files.
    This allows you to update markdown generation without re-downloading.
    """
    print("\n📝 Regenerating markdown from raw emails...\n")

    # Ensure output directories exist
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(attachments_dir, exist_ok=True)

    # Find all .eml files in all folder subdirectories
    eml_files = []
    for root, dirs, files in os.walk(raw_emails_dir):
        for file in files:
            if file.endswith('.eml'):
                eml_files.append(Path(root) / file)

    if len(eml_files) == 0:
        print("   ⚠️  No raw emails found. Run download first.")
        return 0

    print(f"   Found {len(eml_files)} raw emails across all folders")

    # Sort by filename (which starts with date) for chronological processing
    eml_files.sort(key=lambda p: p.name)

    # Generate markdown files
    exported = 0
    skipped = 0

    for eml_file in tqdm(eml_files, desc="  Generating markdown", unit="file"):
        if shutdown_event.is_set():
            print("\n   ⚠️  Cancelled by user. Stopping...")
            break

        try:
            markdown, metadata, _ = create_markdown_from_raw_email(
                str(eml_file), attachments_dir, remove_quotes
            )

            # Skip contentless forwards if quote removal is enabled
            if remove_quotes:
                body_start = markdown.find("---\n\n") + 5
                body_only = markdown[body_start:].strip()

                # Check if body is too short
                if len(body_only) < 10:
                    skipped += 1
                    continue

                # Check if it's just a forward header with no content
                # Common patterns: "Begin forwarded message:", "Forwarding...", etc.
                body_lower = body_only.lower()
                if (body_lower.startswith('begin forwarded message') or
                    body_lower.startswith('forwarded message') or
                    body_lower == 'forwarding' or
                    (body_lower.startswith('fwd:') and len(body_only) < 50)):
                    skipped += 1
                    continue

            # Save markdown (extract UID from filename if present)
            uid_match = re.search(r'_(\d+)_[^_]+\.eml$', eml_file.name)
            imap_uid = uid_match.group(1) if uid_match else None

            filename_base = get_email_filename(metadata, imap_uid)
            filepath_md = os.path.join(output_dir, sanitize_filename(filename_base) + ".md")

            with open(filepath_md, "w", encoding="utf-8") as f:
                f.write(markdown)

            exported += 1
        except Exception as e:
            print(f"\n⚠️  Failed to generate markdown for {eml_file.name}: {e}")
            skipped += 1

    print(f"\n   ✅ Generated {exported} markdown files")
    if skipped > 0:
        print(f"   🗑️  Skipped {skipped} contentless emails")

    return exported


def main():
    """Main function to run the email export tool."""
    print("📧 Email Export Tool v2 (Sync + Raw + Markdown)")
    print()

    # Get configuration
    config = get_configuration()
    print()

    # Create output directories
    os.makedirs(config['output_dir'], exist_ok=True)
    raw_emails_dir = os.path.join(config['output_dir'], "raw_emails")
    os.makedirs(raw_emails_dir, exist_ok=True)
    markdown_dir = os.path.join(config['output_dir'], "markdown")
    os.makedirs(markdown_dir, exist_ok=True)
    attachments_dir = os.path.join(config['output_dir'], "attachments")
    os.makedirs(attachments_dir, exist_ok=True)

    # Ask what to do
    print("What would you like to do?")
    print("  1. Sync new emails from IMAP (download only new/changed)")
    print("  2. Regenerate markdown from existing raw emails")
    print("  3. Both (sync + regenerate)")
    choice = input("\nChoice (1/2/3): ").strip()
    print()

    downloaded_count = 0

    if choice in ['1', '3']:
        # Connect to server
        try:
            mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
            mail.login(config['email'], config['password'])
            print("✅ Connected successfully.")
        except Exception as e:
            print(f"❌ Connection failed: {e}")
            return 1

        # Sync emails (only download new ones)
        downloaded = download_emails_from_imap(
            mail, config['imap_folders'], raw_emails_dir,
            include_sent_folder=config['include_related_sent']
        )

        mail.close()
        mail.logout()

        downloaded_count = len(downloaded)
        print(f"📥 Synced {downloaded_count} new emails\n")

    if choice in ['2', '3']:
        # Regenerate markdown
        exported_count = regenerate_all_markdown(
            raw_emails_dir, markdown_dir, attachments_dir,
            config['remove_quotes']
        )
    else:
        exported_count = 0

    # Summary
    print("\n" + "=" * 60)
    print("Export complete!")
    print("=" * 60)
    if choice in ['1', '3']:
        print(f"New emails downloaded: {downloaded_count}")
    if choice in ['2', '3']:
        print(f"Markdown files generated: {exported_count}")
    print(f"Raw emails stored in: {raw_emails_dir}")
    print("   (organized by folder: Important_Estate/, Sent/, etc.)")
    print(f"Markdown output: {markdown_dir}")
    print(f"Attachments: {attachments_dir}")
    print("\nTip: Run option 2 to regenerate markdown after tweaking")
    print("   quote removal logic - no need to re-download!")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
