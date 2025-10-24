import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import os
import re
import json
import sys
import signal
import logging
import argparse
from getpass import getpass
from tqdm import tqdm
import keyring
from threading import Event
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List
from concurrent.futures import ProcessPoolExecutor, as_completed

try:
    from pymarkdown.api import PyMarkdownApi, PyMarkdownApiException
    MARKDOWN_LINTER_AVAILABLE = True
except ImportError:
    MARKDOWN_LINTER_AVAILABLE = False

CONFIG_FILE = "mail_config.json"
KEYRING_SERVICE = "MailToMarkdown"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mail_export.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Global shutdown event for graceful termination
shutdown_event = Event()

# Constants for magic numbers
MIN_CONTENT_LENGTH = 10
MAX_SIGNATURE_LENGTH = 30
MIN_CONSECUTIVE_QUOTES = 3
MIN_COMMON_SUBSTRING_LENGTH = 100


@dataclass
class EmailPatterns:
    """
    Centralized email pattern detection with compiled regex patterns.
    This eliminates code duplication and improves performance by compiling patterns once.
    Supports custom user patterns from configuration.
    """

    # Reply separator patterns - compiled once at class definition
    REPLY_PATTERNS: List[re.Pattern] = None
    FORWARD_PATTERNS: List[re.Pattern] = None
    SIGNATURE_PATTERNS: List[re.Pattern] = None
    FORWARD_SUBJECT_PREFIXES: List[str] = None

    # Custom patterns from user config
    CUSTOM_REPLY_PATTERNS: List[str] = None
    CUSTOM_FORWARD_PATTERNS: List[str] = None
    CUSTOM_SIGNATURE_PATTERNS: List[str] = None

    def __post_init__(self):
        """Initialize patterns if not already set."""
        if EmailPatterns.REPLY_PATTERNS is None:
            EmailPatterns._initialize_patterns()

    @classmethod
    def load_custom_patterns(cls, config: dict):
        """
        Load custom patterns from user configuration.

        Args:
            config: Configuration dictionary with optional 'pattern_detection' section
        """
        pattern_config = config.get('pattern_detection', {})

        if pattern_config:
            logger.info("Loading custom pattern detection configuration")

            # Store custom patterns
            cls.CUSTOM_REPLY_PATTERNS = pattern_config.get('custom_reply_patterns', [])
            cls.CUSTOM_FORWARD_PATTERNS = pattern_config.get('custom_forward_patterns', [])
            cls.CUSTOM_SIGNATURE_PATTERNS = pattern_config.get('custom_signature_patterns', [])

            # Reinitialize patterns to include custom ones
            cls._initialize_patterns()

            logger.info(f"Loaded {len(cls.CUSTOM_REPLY_PATTERNS or [])} custom reply patterns")
            logger.info(f"Loaded {len(cls.CUSTOM_FORWARD_PATTERNS or [])} custom forward patterns")
            logger.info(f"Loaded {len(cls.CUSTOM_SIGNATURE_PATTERNS or [])} custom signature patterns")

    @classmethod
    def _initialize_patterns(cls):
        """Initialize all compiled regex patterns including custom user patterns."""
        # Reply separator patterns - English only for now
        reply_pattern_strings = [
            # Standard: "On [date] at [time], Name <email> wrote:"
            r'^On\s+.+?\d+.*?at\s+\d+.+?wrote:?\s*$',

            # Without 'at': "On [date], Name wrote:"
            r'^On\s+.+?\d+.+?wrote:?\s*$',

            # With email: "On [date] Name <email@domain.com> wrote:"
            r'^On\s+.+?\d+.*?<.+?@.+?>\s+wrote:?\s*$',

            # Outlook-style headers
            r'^From:\s*.+$',
            r'^Sent:\s*.+$',
            r'^Date:\s*.+$',
        ]

        # Add custom reply patterns from user config
        if cls.CUSTOM_REPLY_PATTERNS:
            reply_pattern_strings.extend(cls.CUSTOM_REPLY_PATTERNS)

        # Compile all reply patterns
        cls.REPLY_PATTERNS = [
            re.compile(pattern, re.IGNORECASE) for pattern in reply_pattern_strings
        ]

        # Forward message patterns
        forward_pattern_strings = [
            r'^[-=]{5,}\s*Forwarded\s+[Mm]essage\s*[-=]{5,}',
            r'^Begin\s+forwarded\s+message:',
            r'^Forwarded\s+message',
            r'^-+\s*Original\s+[Mm]essage\s*-+',
        ]

        # Add custom forward patterns
        if cls.CUSTOM_FORWARD_PATTERNS:
            forward_pattern_strings.extend(cls.CUSTOM_FORWARD_PATTERNS)

        cls.FORWARD_PATTERNS = [
            re.compile(pattern, re.IGNORECASE) for pattern in forward_pattern_strings
        ]

        # Signature patterns
        signature_pattern_strings = [
            # Dash-based: "- Name", "-- Name"
            r'^-+\s*[\w\s]{1,20}$',

            # Common closings
            r'^(Best regards?|Thanks?|Cheers|Sincerely|Regards|Kind regards),?\s*$',

            # Mobile signatures
            r'^Sent from my \w+',
            r'^Get Outlook for \w+',
            r'^Sent from \w+',
        ]

        # Add custom signature patterns
        if cls.CUSTOM_SIGNATURE_PATTERNS:
            signature_pattern_strings.extend(cls.CUSTOM_SIGNATURE_PATTERNS)

        cls.SIGNATURE_PATTERNS = [
            re.compile(pattern, re.IGNORECASE) for pattern in signature_pattern_strings
        ]

        # Forward subject prefixes (English only)
        cls.FORWARD_SUBJECT_PREFIXES = [
            'fwd:', 'fw:', 'fwd_', 'fw_', '[fwd:', 'forward:',
        ]

    @classmethod
    def detect_reply_separator(cls, body: str) -> Optional[int]:
        """
        Detect where quoted/replied content starts in an email body.
        Returns the line index where quoting begins, or None if not found.

        Optimized with early exits:
        - Only checks first 30 lines (reply separators are typically near the top)
        - Skips empty lines at the start
        """
        if not body:
            return None

        lines = body.split('\n')

        # OPTIMIZATION: Reply separators are typically in the first 30 lines
        # Most emails don't have them deeper than this
        max_lines_to_check = min(30, len(lines))

        for i in range(max_lines_to_check):
            stripped = lines[i].strip()

            # Early exit: Skip leading empty lines
            if not stripped and i < 3:
                continue

            # Check reply patterns (early exit on first match)
            for pattern in cls.REPLY_PATTERNS:
                if pattern.match(stripped):
                    return i

            # Check forward patterns (early exit on first match)
            for pattern in cls.FORWARD_PATTERNS:
                if pattern.match(stripped):
                    return i

            # Pattern: Multiple consecutive lines starting with >
            # (need at least MIN_CONSECUTIVE_QUOTES to avoid false positives with blockquotes)
            if stripped.startswith('>'):
                consecutive_quotes = 1
                for j in range(i + 1, min(i + 4, max_lines_to_check)):
                    if lines[j].strip().startswith('>'):
                        consecutive_quotes += 1

                if consecutive_quotes >= MIN_CONSECUTIVE_QUOTES:
                    return i

        return None

    @classmethod
    def is_signature_line(cls, line: str) -> bool:
        """
        Check if a line looks like an email signature.
        Returns True if the line matches common signature patterns.
        """
        if not line:
            return False

        stripped = line.strip()

        # Check against signature patterns
        for pattern in cls.SIGNATURE_PATTERNS:
            if pattern.match(stripped):
                return True

        return False

    @classmethod
    def is_forward_subject(cls, subject: str) -> bool:
        """
        Check if subject indicates a forwarded message.
        Handles various formats and localizations.
        """
        if not subject:
            return False

        subject_lower = subject.lower().strip()

        return any(subject_lower.startswith(prefix) for prefix in cls.FORWARD_SUBJECT_PREFIXES)


# Initialize patterns at module load
_patterns = EmailPatterns()
_patterns._initialize_patterns()


def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully."""
    print("\n\n[Warning]  Interrupt received! Shutting down gracefully...")
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

    DEPRECATED: This function now delegates to EmailPatterns.detect_reply_separator()
    for centralized pattern matching. Kept for backward compatibility.
    """
    return EmailPatterns.detect_reply_separator(body)


def is_contentless_forward(metadata, body):
    """Check if this is a forward with no original content."""
    subject = metadata.get('subject', '').strip()

    # Use centralized forward detection
    if not EmailPatterns.is_forward_subject(subject):
        return False

    # Check if there's any content before the reply separator
    separator_idx = detect_reply_separator(body)

    if separator_idx is None:
        # No separator found, check body length
        return len(body.strip()) < MIN_CONTENT_LENGTH

    # Check content before separator
    lines = body.split('\n')
    original_content = '\n'.join(lines[:separator_idx]).strip()

    return len(original_content) < MIN_CONTENT_LENGTH


def load_config():
    """Load saved configuration from file and initialize custom patterns."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # Load custom patterns if present
            if 'pattern_detection' in config:
                logger.info("Custom pattern configuration found in config file")
                EmailPatterns.load_custom_patterns(config)

            return config
        except Exception as e:
            logger.error(f"Could not load config: {e}", exc_info=True)
            print(f"[Warning] Could not load config: {e}")
    return {}


def save_config(config):
    """Save configuration to file (excluding password)."""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        print(f"[Save] Configuration saved to {CONFIG_FILE}")
    except Exception as e:
        print(f"[Warning] Could not save config: {e}")


def get_saved_password(email_addr):
    """Retrieve password from system keyring."""
    try:
        return keyring.get_password(KEYRING_SERVICE, email_addr)
    except Exception as e:
        print(f"[Warning] Could not retrieve password: {e}")
        return None


def save_password(email_addr, password):
    """Save password to system keyring."""
    try:
        keyring.set_password(KEYRING_SERVICE, email_addr, password)
        print("[Security] Password saved securely to system keyring")
    except Exception as e:
        print(f"[Warning] Could not save password: {e}")


def get_configuration():
    """Get configuration from user input or saved config."""
    config = load_config()
    use_saved = False

    if config:
        print("[Config] Found saved configuration:")
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
            print("[Key] Using saved password")
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

        print("\n[Folder] Folder Selection:")
        print("   Primary folder to export (e.g., INBOX, Important/Estate)")
        imap_folders = [input("Primary IMAP folder: ").strip() or "INBOX"]

        print("\n[Sent] Include Related Sent Emails:")
        print("   Export sent emails that are replies to emails in the primary folder?")
        include_related_sent_input = input("Include related sent emails? (y/n): ").strip().lower()
        include_related_sent = include_related_sent_input in ['y', 'yes']

        print("\n[Clean] Remove Quoted Replies:")
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


def build_chronological_body_fingerprints(raw_emails_dir: str) -> dict:
    """
    Build a chronological index of email body fingerprints for quote detection.

    This function:
    1. Scans all emails and sorts them by date
    2. Creates normalized body text fingerprints
    3. Stores them indexed by Message-ID for quick lookup

    Args:
        raw_emails_dir: Directory containing raw email files

    Returns:
        Dict with structure:
        {
            'message_id': {
                'date': datetime object,
                'body': normalized body text,
                'message_id': the Message-ID,
                'in_reply_to': parent Message-ID if any
            }
        }
    """
    cache_file = os.path.join(raw_emails_dir, '.body_fingerprints.json')
    fingerprints = {}

    # Load existing cache
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            logger.info(f"Loaded fingerprint cache with {len(cache_data)} entries")
            # Convert date strings back to timestamps for sorting
            for msg_id, data in cache_data.items():
                fingerprints[msg_id] = data
        except Exception as e:
            logger.warning(f"Failed to load fingerprint cache: {e}")
            fingerprints = {}

    # Scan all emails
    emails_to_process = []
    for root, dirs, files in os.walk(raw_emails_dir):
        for filename in files:
            if filename.endswith('.eml'):
                eml_path = os.path.join(root, filename)
                try:
                    file_mtime = os.path.getmtime(eml_path)
                    rel_path = os.path.relpath(eml_path, raw_emails_dir).replace('\\', '/')

                    # Check if we need to reprocess this file
                    with open(eml_path, 'rb') as f:
                        msg = email.message_from_bytes(f.read())
                        message_id = msg.get("Message-ID", "")

                        if not message_id:
                            continue

                        # Check cache
                        if message_id in fingerprints:
                            cached_mtime = fingerprints[message_id].get('mtime', 0)
                            if cached_mtime == file_mtime:
                                continue  # Already cached and up to date

                        # Need to process
                        emails_to_process.append((eml_path, file_mtime, msg, message_id))

                except Exception as e:
                    logger.warning(f"Failed to check {filename}: {e}")

    # Process new/updated emails
    if emails_to_process:
        logger.info(f"Processing {len(emails_to_process)} emails for fingerprinting")

        for eml_path, file_mtime, msg, message_id in emails_to_process:
            try:
                # Get date
                date_str = msg.get("Date", "")
                try:
                    date_obj = parsedate_to_datetime(date_str)
                    date_timestamp = date_obj.timestamp()
                    date_iso = date_obj.isoformat()
                except:
                    date_timestamp = 0
                    date_iso = "1970-01-01T00:00:00"

                # Get In-Reply-To
                in_reply_to = msg.get("In-Reply-To", "")

                # Extract plain text body
                body_text = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload and isinstance(payload, bytes):
                                body_text = payload.decode(errors='ignore')
                                break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload and isinstance(payload, bytes):
                        body_text = payload.decode(errors='ignore')

                # Normalize body for matching (lowercase, remove extra whitespace)
                normalized_body = re.sub(r'\s+', ' ', body_text.strip().lower())

                # Store fingerprint
                fingerprints[message_id] = {
                    'date': date_iso,
                    'date_timestamp': date_timestamp,
                    'body': normalized_body,
                    'raw_body': body_text,  # Keep raw for reconstruction
                    'message_id': message_id,
                    'in_reply_to': in_reply_to,
                    'mtime': file_mtime
                }

            except Exception as e:
                logger.warning(f"Failed to fingerprint {eml_path}: {e}")

    # Save cache
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(fingerprints, f, indent=2)
        logger.info(f"Saved fingerprint cache with {len(fingerprints)} entries")
    except Exception as e:
        logger.warning(f"Failed to save fingerprint cache: {e}")

    logger.info(f"Fingerprint index ready with {len(fingerprints)} emails")
    return fingerprints


def find_matching_blocks_levenshtein(text_current: str, text_previous: str, min_block_size: int = 50):
    """
    Find large matching blocks between current and previous email using difflib.

    This uses the SequenceMatcher to find matching blocks, similar to Levenshtein
    distance but more focused on finding exact substring matches.

    Args:
        text_current: The current email body (normalized)
        text_previous: The previous email body (normalized)
        min_block_size: Minimum size of a block to be considered (in characters)

    Returns:
        List of tuples: (start_in_current, start_in_previous, length)
    """
    from difflib import SequenceMatcher

    matcher = SequenceMatcher(None, text_current, text_previous)
    matching_blocks = []

    for block in matcher.get_matching_blocks():
        if block.size >= min_block_size:
            matching_blocks.append((block.a, block.b, block.size))

    return matching_blocks


def remove_quoted_content_by_fingerprint(email_body: str, message_id: str,
                                         fingerprints: dict) -> str:
    """
    Remove quoted content from email body using fingerprint matching.

    Strategy:
    1. Look up ALL chronologically previous emails (not just direct parent)
    2. Find large matching blocks between current and previous emails
    3. If a large continuous block (>70% of previous email) is found, remove it
    4. If blocks are fragmented (<70% in largest block), preserve as inline reply
    5. Remove matching blocks only if they appear to be pure quotes, not interwoven content

    This approach handles:
    - Missing or incorrect In-Reply-To headers
    - Forwards that quote previous content
    - Multi-level thread quoting

    Args:
        email_body: Raw email body text
        message_id: Message-ID of current email
        fingerprints: Dict of email fingerprints indexed by Message-ID

    Returns:
        Cleaned email body with quoted content removed
    """
    # Get metadata for current email
    if message_id not in fingerprints:
        return email_body  # Can't process, return as-is

    current_data = fingerprints[message_id]
    current_date_timestamp = current_data.get('date_timestamp', 0)
    current_body_normalized = current_data['body']

    if len(current_body_normalized) < 50:
        return email_body  # Too short to analyze

    # Get all emails that came BEFORE this one chronologically
    # Limit to recent emails (within 90 days) to avoid checking thousands of old emails
    time_window = 90 * 24 * 60 * 60  # 90 days in seconds
    cutoff_timestamp = current_date_timestamp - time_window

    previous_emails = []
    for msg_id, data in fingerprints.items():
        if msg_id == message_id:
            continue

        prev_timestamp = data.get('date_timestamp', 0)
        if prev_timestamp < current_date_timestamp and prev_timestamp > cutoff_timestamp:
            prev_body = data.get('body', '')
            if len(prev_body) >= 50:  # Only consider emails with substantial content
                previous_emails.append({
                    'message_id': msg_id,
                    'body': prev_body,
                    'timestamp': prev_timestamp
                })

    if not previous_emails:
        return email_body  # No previous emails to check against

    # Sort by date (most recent first) to prioritize checking immediate parents
    previous_emails.sort(key=lambda x: x['timestamp'], reverse=True)

    # Limit to most recent 50 emails for performance
    # (Most quotes reference very recent emails anyway)
    previous_emails = previous_emails[:50]

    # Check for matches against all previous emails
    best_match = None
    best_match_ratio = 0

    for prev_email in previous_emails:
        prev_body_normalized = prev_email['body']

        # Find matching blocks with this previous email
        matching_blocks = find_matching_blocks_levenshtein(
            current_body_normalized,
            prev_body_normalized,
            min_block_size=50
        )

        if not matching_blocks:
            continue

        # Analyze the matching blocks
        total_prev_length = len(prev_body_normalized)
        total_matched = sum(block[2] for block in matching_blocks)
        largest_block = max(matching_blocks, key=lambda x: x[2])
        largest_block_size = largest_block[2]

        # Calculate ratios
        largest_block_ratio = largest_block_size / total_prev_length
        total_match_ratio = total_matched / total_prev_length

        # Keep track of the best (highest ratio) match
        if largest_block_ratio > best_match_ratio:
            best_match_ratio = largest_block_ratio
            best_match = {
                'prev_email': prev_email,
                'matching_blocks': matching_blocks,
                'largest_block': largest_block,
                'largest_block_ratio': largest_block_ratio,
                'total_match_ratio': total_match_ratio,
                'total_prev_length': total_prev_length
            }

        # Early exit: if we found a very strong match (>80%), no need to check further
        if largest_block_ratio >= 0.80:
            break

    if not best_match:
        return email_body  # No significant matches found

    # Use the best match for quote removal decision
    largest_block_ratio = best_match['largest_block_ratio']
    total_match_ratio = best_match['total_match_ratio']
    largest_block = best_match['largest_block']
    total_prev_length = best_match['total_prev_length']
    prev_msg_id = best_match['prev_email']['message_id']

    logger.debug(f"Quote analysis for {message_id[:20]}... (matched against {prev_msg_id[:20]}...)")
    logger.debug(f"  Largest block: {largest_block_ratio:.0%} ({largest_block[2]}/{total_prev_length})")
    logger.debug(f"  Total match: {total_match_ratio:.0%}")

    # Decision logic:
    # Check for short top-posts FIRST (before other heuristics)
    # If new content is very short (<500 chars) with a clear separator, likely a brief reply
    separator_idx = EmailPatterns.detect_reply_separator(email_body)
    if separator_idx is not None:
        lines = email_body.split('\n')
        new_content = '\n'.join(lines[:separator_idx]).strip()

        # Short top-post: brief message with quotes below
        if len(new_content) < 500 and separator_idx < len(lines) - 5:
            content_lines = lines[:separator_idx]
            while content_lines and not content_lines[-1].strip():
                content_lines.pop()
            while content_lines and EmailPatterns.is_signature_line(content_lines[-1].strip()):
                content_lines.pop()
                while content_lines and not content_lines[-1].strip():
                    content_lines.pop()

            cleaned_body = '\n'.join(content_lines).strip()
            if len(cleaned_body) >= MIN_CONTENT_LENGTH:
                logger.info(f"Removed quotes from short top-post {message_id[:20]}... ({len(email_body)} -> {len(cleaned_body)} chars)")
                return cleaned_body

    # If >=70% of parent appears in ONE continuous block -> likely full quote, remove it
    # If blocks are fragmented (largest <70%) -> likely inline reply, preserve it

    if largest_block_ratio >= 0.70:
        # This is likely a traditional top-post with full quote
        # Use reply separator detection to find where to cut

        separator_idx = EmailPatterns.detect_reply_separator(email_body)

        if separator_idx is not None:
            lines = email_body.split('\n')
            # Take everything before the separator
            content_lines = lines[:separator_idx]

            # Remove trailing empty lines
            while content_lines and not content_lines[-1].strip():
                content_lines.pop()

            # Remove signature lines at the end
            while content_lines and EmailPatterns.is_signature_line(content_lines[-1].strip()):
                content_lines.pop()
                # Remove empty lines again after signature
                while content_lines and not content_lines[-1].strip():
                    content_lines.pop()

            cleaned_body = '\n'.join(content_lines).strip()

            # Ensure we kept enough content
            if len(cleaned_body) >= MIN_CONTENT_LENGTH:
                logger.info(f"Removed full quote block from {message_id[:20]}... ({len(email_body)} -> {len(cleaned_body)} chars)")
                return cleaned_body

        # Fallback: No separator found, but we know there's a large quote block
        # Try to find the quote start by looking for common indicators
        lines = email_body.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Look for lines that start with "On ... wrote:" or similar
            if stripped.startswith('On ') and (' wrote:' in stripped or 'wrote:' in stripped):
                # Cut here
                content_lines = lines[:i]
                while content_lines and not content_lines[-1].strip():
                    content_lines.pop()

                cleaned_body = '\n'.join(content_lines).strip()
                if len(cleaned_body) >= MIN_CONTENT_LENGTH:
                    logger.info(f"Removed quote block (no separator) from {message_id[:20]}... ({len(email_body)} -> {len(cleaned_body)} chars)")
                    return cleaned_body
                break

        logger.debug(f"Large quote detected but couldn't find safe cut point, keeping original")
        return email_body

    elif total_match_ratio >= 0.15:
        # Fragmented quotes - this is an inline reply
        logger.debug(f"Inline reply detected (fragmented quotes), preserving all content")
        return email_body

    else:
        # Very little matching content, not quoting significantly
        return email_body


def extract_email_body_and_attachments(msg, attachments_dir):
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
                        print(f"[Warning]  Skipped .eml attachment with no content: {filename}")
                else:
                    # Normal attachment handling
                    payload = part.get_payload(decode=True)
                    if payload is not None:  # Check if payload is valid
                        with open(filepath, "wb") as f:
                            f.write(payload)
                        attachment_paths.append(f"[{filename}](attachments/{filename})")
                        attachment_count += 1
                    else:
                        print(f"[Warning]  Skipped attachment with no content: {filename}")
            except Exception as e:
                print(f"[Warning]  Failed to save attachment: {filename} → {e}")
        elif content_type == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                raw_body = payload.decode(errors="ignore")
                body += raw_body

    return body, attachment_paths, attachment_count


def create_markdown_from_raw_email(eml_filepath, attachments_dir, remove_quotes=True, body_index=None):
    """
    Create markdown content from a raw .eml file.
    This allows re-processing without re-downloading from IMAP.
    
    Args:
        eml_filepath: Path to the .eml file
        attachments_dir: Directory to save attachments
        remove_quotes: Whether to remove quoted replies
        body_index: Optional dict mapping Message-ID to body content for inline reply detection
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

    # Extract body
    body, attachment_paths, _ = extract_email_body_and_attachments(
        msg, attachments_dir
    )

    # Apply fingerprint-based quote removal if enabled and fingerprints available
    if remove_quotes and body_index is not None:
        message_id = metadata.get('message_id', '')
        if message_id:
            body = remove_quoted_content_by_fingerprint(body, message_id, body_index)

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


def validate_and_fix_markdown(markdown_content: str, filepath: str = "", auto_fix: bool = True) -> tuple:
    """
    Validate and optionally fix markdown content using pymarkdownlnt.

    Args:
        markdown_content: The markdown content to validate
        filepath: Optional filepath for better error reporting
        auto_fix: Whether to automatically fix issues (default True)

    Returns:
        Tuple of (fixed_markdown: str, was_fixed: bool, issues: list)
        - fixed_markdown: The corrected markdown (or original if not fixed)
        - was_fixed: True if fixes were applied
        - issues: List of issues found (before fixing)
    """
    if not MARKDOWN_LINTER_AVAILABLE:
        logger.debug("Markdown linter not available, skipping validation")
        return markdown_content, False, []

    try:
        api = PyMarkdownApi()

        # Configure linter to be more lenient for email content
        # Disable rules that are too strict for email markdown
        api.disable_rule_by_identifier("line-length")  # Email lines can be long
        api.disable_rule_by_identifier("no-trailing-punctuation")  # Subject lines may have punctuation
        api.disable_rule_by_identifier("no-duplicate-heading")  # Emails may repeat subjects
        api.disable_rule_by_identifier("single-trailing-newline")  # Not critical for emails

        # First scan to identify issues
        scan_result = api.scan_string(markdown_content)

        # Collect all issues
        all_issues = []

        for failure in scan_result.scan_failures:
            issue = {
                'line': failure.line_number,
                'column': failure.column_number,
                'rule': failure.rule_id,
                'message': failure.rule_description
            }
            all_issues.append(issue)

        for error in scan_result.pragma_errors:
            issue = {
                'line': error.line_number,
                'column': 0,
                'rule': 'pragma',
                'message': str(error)
            }
            all_issues.append(issue)

        for error in scan_result.critical_errors:
            issue = {
                'line': 0,
                'column': 0,
                'rule': 'critical',
                'message': error
            }
            all_issues.append(issue)

        # If issues found and auto_fix enabled, attempt to fix them
        if all_issues and auto_fix:
            fix_result = api.fix_string(markdown_content)
            if fix_result.was_fixed:
                if filepath:
                    logger.info(f"Markdown auto-fixed: {len(all_issues)} issues in {filepath}")
                return fix_result.fixed_file, True, all_issues

        # Return original markdown if no fixes needed or auto_fix disabled
        if all_issues and filepath:
            logger.warning(f"Markdown validation: {len(all_issues)} issues in {filepath}")

        return markdown_content, False, all_issues

    except PyMarkdownApiException as e:
        logger.error(f"Markdown validation error: {e}")
        return markdown_content, False, []  # Return original on error
    except Exception as e:
        logger.error(f"Unexpected markdown validation error: {e}")
        return markdown_content, False, []


def scan_existing_message_ids(raw_emails_dir: str, imap_folders: List[str]) -> set:
    """
    Scan existing emails in primary folders to collect their Message-IDs.
    Uses cached index for massive performance improvement on subsequent runs.

    Args:
        raw_emails_dir: Directory containing raw email files
        imap_folders: List of primary folder names to scan

    Returns:
        Set of Message-IDs found in existing emails
    """
    cache_file = os.path.join(raw_emails_dir, '.message_id_cache.json')
    cache = {}

    # Load existing cache
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            logger.info(f"Loaded Message-ID cache with {len(cache)} entries")
        except Exception as e:
            logger.warning(f"Failed to load cache, will rebuild: {e}")
            cache = {}

    primary_message_ids = set()
    files_to_scan = []
    cache_hits = 0

    logger.info("Scanning existing emails for Message-IDs")

    for folder_name in imap_folders:
        folder_subdir = get_folder_subdir(folder_name)
        folder_path = os.path.join(raw_emails_dir, folder_subdir)

        if os.path.exists(folder_path):
            for filename in os.listdir(folder_path):
                if filename.endswith('.eml'):
                    eml_path = os.path.join(folder_path, filename)

                    try:
                        # Get file modification time for cache validation
                        file_mtime = os.path.getmtime(eml_path)
                        cache_key = f"{folder_name}/{filename}"

                        # Check if in cache and not modified
                        if cache_key in cache and cache[cache_key].get('mtime') == file_mtime:
                            # Use cached Message-ID
                            message_id = cache[cache_key].get('message_id')
                            if message_id:
                                primary_message_ids.add(message_id)
                                cache_hits += 1
                        else:
                            # Need to scan this file
                            files_to_scan.append((cache_key, eml_path, file_mtime))
                    except Exception as e:
                        logger.warning(f"Failed to check {filename}: {e}")

    # Scan files not in cache or modified
    if files_to_scan:
        logger.info(f"Scanning {len(files_to_scan)} new/modified files (cached: {cache_hits})")
        for cache_key, eml_path, file_mtime in files_to_scan:
            try:
                with open(eml_path, 'rb') as f:
                    msg = email.message_from_bytes(f.read())
                    message_id = msg.get("Message-ID", "")
                    if message_id:
                        primary_message_ids.add(message_id)
                        # Update cache
                        cache[cache_key] = {
                            'message_id': message_id,
                            'mtime': file_mtime
                        }
            except Exception as e:
                logger.warning(f"Failed to read {eml_path}: {e}")
    else:
        logger.info(f"All {cache_hits} files loaded from cache")

    # Save updated cache
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2)
        logger.debug(f"Saved cache with {len(cache)} entries")
    except Exception as e:
        logger.warning(f"Failed to save cache: {e}")

    logger.info(f"Found {len(primary_message_ids)} Message-IDs ({cache_hits} cached, {len(files_to_scan)} scanned)")
    return primary_message_ids


def download_folder_emails(mail, folder_name: str, raw_emails_dir: str,
                           primary_message_ids: set) -> tuple:
    """
    Download new emails from a single IMAP folder.

    Args:
        mail: IMAP connection object
        folder_name: Name of the folder to download from
        raw_emails_dir: Directory to save raw emails
        primary_message_ids: Set to collect Message-IDs (updated in place)

    Returns:
        Tuple of (downloaded_count, downloaded_metadata_list)
    """
    logger.info(f"Processing folder: {folder_name}")
    print(f"[Dir] Syncing folder: {folder_name}")

    downloaded_emails = []

    # Select folder in read-only mode
    try:
        status, _ = mail.select(folder_name, readonly=True)
        if status != 'OK':
            logger.warning(f"Failed to select folder '{folder_name}'")
            print(f"   [Warning]  Failed to select folder '{folder_name}'. Skipping...")
            return 0, []
    except Exception as e:
        logger.error(f"Error accessing folder '{folder_name}': {e}")
        print(f"   [Warning]  Error accessing folder '{folder_name}': {e}. Skipping...")
        return 0, []

    # Get all UIDs from server
    try:
        status, messages = mail.uid('search', None, "ALL")
        if status != 'OK':
            logger.warning(f"Failed to search folder '{folder_name}'")
            print(f"   [Warning]  Failed to search folder '{folder_name}'. Skipping...")
            return 0, []
        server_uids = set(messages[0].split())
    except Exception as e:
        logger.error(f"Error searching folder '{folder_name}': {e}")
        print(f"   [Warning]  Error searching folder '{folder_name}': {e}. Skipping...")
        return 0, []

    # Get UIDs we already have locally
    local_uids = get_local_email_uids(raw_emails_dir, folder_name)

    # Convert to comparable format (bytes to strings)
    server_uids_str = {uid.decode() if isinstance(uid, bytes) else str(uid) for uid in server_uids}

    # Find UIDs we need to download
    uids_to_download = server_uids_str - local_uids

    print(f"   [Mail] Server has {len(server_uids_str)} emails")
    print(f"   [Save] Local has {len(local_uids)} emails")
    print(f"   [Download] Need to download {len(uids_to_download)} new emails")

    if len(uids_to_download) == 0:
        print(f"   [OK] '{folder_name}' is up to date\n")
        return 0, []

    # Download new emails
    folder_downloaded = 0
    for uid in tqdm(uids_to_download, desc=f"  Downloading from {folder_name}", unit="email"):
        if shutdown_event.is_set():
            logger.warning("Download cancelled by user")
            print("\n   [Warning]  Cancelled by user. Stopping download...")
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
            logger.error(f"Failed to download email UID {uid}: {e}", exc_info=True)
            print(f"\n[Warning]  Failed to download email UID {uid}: {e}")

    logger.info(f"Downloaded {folder_downloaded} new emails from '{folder_name}'")
    print(f"   [OK] Downloaded {folder_downloaded} new emails from '{folder_name}'\n")

    return folder_downloaded, downloaded_emails


def download_related_sent_emails(mail, raw_emails_dir: str,
                                 primary_message_ids: set) -> tuple:
    """
    Download sent emails that are replies to primary folder emails.

    Args:
        mail: IMAP connection object
        raw_emails_dir: Directory to save raw emails
        primary_message_ids: Set of Message-IDs from primary emails

    Returns:
        Tuple of (downloaded_count, downloaded_metadata_list)
    """
    logger.info("Syncing related emails from Sent folder")
    print("[Sent] Syncing related emails from Sent folder...")
    print(f"   Looking for replies to {len(primary_message_ids)} primary emails")

    downloaded_emails = []

    try:
        status, _ = mail.select("Sent", readonly=True)
        if status != 'OK':
            logger.warning("Failed to select Sent folder")
            print("   [Warning]  Failed to select Sent folder. Skipping...")
            return 0, []

        # Get all UIDs from Sent folder
        status, messages = mail.uid('search', None, "ALL")
        if status != 'OK':
            logger.warning("Failed to search Sent folder")
            print("   [Warning]  Failed to search Sent folder. Skipping...")
            return 0, []

        server_uids = set(messages[0].split())
        local_uids = get_local_email_uids(raw_emails_dir, "Sent")
        server_uids_str = {
            uid.decode() if isinstance(uid, bytes) else str(uid)
            for uid in server_uids
        }
        uids_to_check = server_uids_str - local_uids

        print(f"   [Mail] Sent folder has {len(server_uids_str)} emails")
        print(f"   [Save] Local has {len(local_uids)} sent emails")
        print(f"   [Search] Checking {len(uids_to_check)} new sent emails for relationships")

        # First pass: fetch only headers to check relationships (much faster)
        related_uids = []
        for uid in tqdm(uids_to_check, desc="  Scanning headers", unit="email"):
            if shutdown_event.is_set():
                logger.warning("Sent folder scan cancelled by user")
                print("\n   [Warning]  Cancelled by user. Stopping scan...")
                break

            try:
                # Fetch only headers (much faster than full email)
                res, msg_data = mail.uid(
                    'fetch', uid,
                    "(BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO REFERENCES)])"
                )
                if res == 'OK' and msg_data and msg_data[0]:
                    header_data = msg_data[0][1]
                    if isinstance(header_data, bytes):
                        header_text = header_data.decode(errors='ignore')

                        # Extract In-Reply-To and References
                        in_reply_to_match = re.search(
                            r'In-Reply-To:\s*(.+?)(?:\r?\n(?!\s)|$)',
                            header_text,
                            re.DOTALL | re.IGNORECASE
                        )
                        references_match = re.search(
                            r'References:\s*(.+?)(?:\r?\n(?!\s)|$)',
                            header_text,
                            re.DOTALL | re.IGNORECASE
                        )

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
                logger.error(f"Failed to check headers for UID {uid}: {e}")
                print(f"\n[Warning]  Failed to check headers for UID {uid}: {e}")

        print(f"   [Download] Found {len(related_uids)} related sent emails")

        # Second pass: download only the related emails
        related_count = 0
        for uid in tqdm(related_uids, desc="  Downloading related", unit="email"):
            if shutdown_event.is_set():
                logger.warning("Related sent download cancelled by user")
                print("\n   [Warning]  Cancelled by user. Stopping download...")
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
                logger.error(f"Failed to download sent email UID {uid}: {e}")
                print(f"\n[Warning]  Failed to download sent email UID {uid}: {e}")

        logger.info(f"Downloaded {related_count} related sent emails")
        print(f"   [OK] Downloaded {related_count} related sent emails\n")

        return related_count, downloaded_emails

    except Exception as e:
        logger.error(f"Error accessing Sent folder: {e}", exc_info=True)
        print(f"   [Warning]  Error accessing Sent folder: {e}. Skipping...")
        return 0, []


def download_emails_from_imap(mail, imap_folders, raw_emails_dir, include_sent_folder=False):
    """
    Sync emails from IMAP server - only download new/changed emails.
    Uses IMAP UIDs to track what we already have.
    Returns list of downloaded email metadata.

    This function coordinates the download process by:
    1. Scanning existing emails for Message-IDs
    2. Downloading new emails from primary folders
    3. Optionally downloading related sent emails
    """
    logger.info("Starting IMAP email sync")
    print("\n[Download] Syncing emails from IMAP...\n")

    # Scan existing emails to get their Message-IDs
    primary_message_ids = scan_existing_message_ids(raw_emails_dir, imap_folders)
    print(f"   [Config] Loaded {len(primary_message_ids)} Message-IDs from existing primary emails\n")

    # Download new emails from each primary folder
    all_downloaded = []
    for folder_name in imap_folders:
        _, folder_emails = download_folder_emails(
            mail, folder_name, raw_emails_dir, primary_message_ids
        )
        all_downloaded.extend(folder_emails)

    # Download related sent emails if requested
    if include_sent_folder and primary_message_ids:
        _, sent_emails = download_related_sent_emails(
            mail, raw_emails_dir, primary_message_ids
        )
        all_downloaded.extend(sent_emails)
    elif include_sent_folder and not primary_message_ids:
        logger.info("No primary emails found, skipping Sent folder sync")
        print("   [Info]  No primary emails found yet, skipping Sent folder sync\n")

    logger.info(f"IMAP sync complete. Downloaded {len(all_downloaded)} total emails")
    return all_downloaded


def _process_single_email_worker(args):
    """
    Worker function for parallel markdown generation.
    Must be at module level for multiprocessing to pickle it.

    Args:
        args: Tuple of (eml_file_path, output_dir, attachments_dir, remove_quotes, MIN_CONTENT_LENGTH, validate,
                        raw_emails_dir, fingerprints)

    Returns:
        Dict with 'exported' bool, 'skipped' bool, 'filename' str, and optional 'error' str
    """
    eml_file, output_dir, attachments_dir, remove_quotes, min_content_length, validate, raw_emails_dir, fingerprints = args

    try:
        markdown, metadata, _ = create_markdown_from_raw_email(
            str(eml_file), attachments_dir, remove_quotes, body_index=fingerprints
        )

        # Skip contentless forwards if quote removal is enabled
        if remove_quotes:
            body_start = markdown.find("---\n\n") + 5
            body_only = markdown[body_start:].strip()

            # Check if body is too short
            if len(body_only) < min_content_length:
                return {'exported': False, 'skipped': True, 'filename': eml_file.name, 'reason': 'too_short'}

            # Check if it's just a forward header with no content
            body_lower = body_only.lower()
            is_empty_forward = (
                body_lower.startswith('begin forwarded message') or
                body_lower.startswith('forwarded message') or
                body_lower == 'forwarding' or
                (body_lower.startswith('fwd:') and len(body_only) < 50)
            )
            if is_empty_forward:
                return {'exported': False, 'skipped': True, 'filename': eml_file.name, 'reason': 'empty_forward'}

        # Validate and fix markdown if requested
        if validate and MARKDOWN_LINTER_AVAILABLE:
            markdown, was_fixed, issues = validate_and_fix_markdown(markdown, eml_file.name, auto_fix=True)
            if issues:
                logger.debug(f"Markdown: {len(issues)} issues in {eml_file.name}, fixed: {was_fixed}")

        # Save markdown (extract UID from filename if present)
        uid_match = re.search(r'_(\d+)_[^_]+\.eml$', eml_file.name)
        imap_uid = uid_match.group(1) if uid_match else None

        filename_base = get_email_filename(metadata, imap_uid)
        filepath_md = os.path.join(output_dir, sanitize_filename(filename_base) + ".md")

        with open(filepath_md, "w", encoding="utf-8") as f:
            f.write(markdown)

        return {'exported': True, 'skipped': False, 'filename': eml_file.name}

    except Exception as e:
        return {'exported': False, 'skipped': False, 'filename': eml_file.name, 'error': str(e)}


def regenerate_all_markdown(
    raw_emails_dir, output_dir, attachments_dir, remove_quotes=True, max_workers=None, validate=False
):
    """
    Regenerate all markdown files from raw .eml files using parallel processing.
    This allows you to update markdown generation without re-downloading.

    Args:
        raw_emails_dir: Directory containing raw .eml files
        output_dir: Directory to save markdown files
        attachments_dir: Directory for attachments
        remove_quotes: Whether to remove quoted replies
        max_workers: Number of parallel workers (None = CPU count)
        validate: Whether to validate generated markdown with pymarkdownlnt

    Returns:
        Number of markdown files exported
    """
    print("\n[Markdown] Regenerating markdown from raw emails (parallel processing)...\n")

    # Ensure output directories exist
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(attachments_dir, exist_ok=True)

    # Build email fingerprints for quote detection (if removing quotes)
    fingerprints = None
    if remove_quotes:
        print("   [Search] Building email body fingerprints for quote detection...")
        fingerprints = build_chronological_body_fingerprints(raw_emails_dir)
        print(f"   [OK] Built fingerprints for {len(fingerprints)} emails\n")

    # Find all .eml files in all folder subdirectories
    eml_files = []
    for root, dirs, files in os.walk(raw_emails_dir):
        for file in files:
            if file.endswith('.eml'):
                eml_files.append(Path(root) / file)

    if len(eml_files) == 0:
        print("   [Warning]  No raw emails found. Run download first.")
        return 0

    print(f"   Found {len(eml_files)} raw emails across all folders")

    # Sort by filename (which starts with date) for chronological processing
    eml_files.sort(key=lambda p: p.name)

    # Prepare worker arguments
    worker_args = [
        (eml_file, output_dir, attachments_dir, remove_quotes, MIN_CONTENT_LENGTH, validate, raw_emails_dir, fingerprints)
        for eml_file in eml_files
    ]

    # Generate markdown files in parallel
    exported = 0
    skipped = 0
    errors = 0

    # Use ProcessPoolExecutor for CPU-bound work
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        futures = {executor.submit(_process_single_email_worker, args): args[0] for args in worker_args}

        # Process results with progress bar
        for future in tqdm(as_completed(futures), total=len(futures), desc="  Generating markdown", unit="file"):
            if shutdown_event.is_set():
                print("\n   [Warning]  Cancelled by user. Stopping...")
                executor.shutdown(wait=False, cancel_futures=True)
                break

            try:
                result = future.result()
                if result['exported']:
                    exported += 1
                elif result['skipped']:
                    skipped += 1
                elif 'error' in result:
                    errors += 1
                    logger.error(f"Failed to process {result['filename']}: {result['error']}")
            except Exception as e:
                errors += 1
                eml_file = futures[future]
                logger.error(f"Worker exception for {eml_file.name}: {e}")

    print(f"\n   [OK] Generated {exported} markdown files")
    if skipped > 0:
        print(f"   [Skip]  Skipped {skipped} contentless emails")
    if errors > 0:
        print(f"   [Warning]  {errors} errors occurred (check logs)")

    return exported


def export_to_json(markdown_dir: str, output_file: str, compressed: bool = False):
    """
    Export markdown emails to JSON format.

    Data structure: Emails stored once, threads reference by ID (no duplication).

    Args:
        markdown_dir: Directory containing markdown files
        output_file: Output JSON file path
        compressed: If True, minify JSON (single line). If False, pretty-print with indentation (default).

    Returns:
        Number of emails exported
    """
    import glob
    from pathlib import Path
    from collections import defaultdict

    print(f"\n[Markdown] Exporting markdown to JSON {'(minified)' if compressed else '(pretty-printed)'}...\n")

    md_files = sorted(glob.glob(os.path.join(markdown_dir, '*.md')))

    if not md_files:
        print("   [Warning] No markdown files found!")
        return 0

    # Image and email extensions to exclude from attachments
    IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.ico', '.webp'}
    EMAIL_EXTS = {'.eml', '.msg'}

    emails = []
    threads = defaultdict(list)

    for md_file in md_files:
        with open(md_file, 'r', encoding='utf-8') as f:
            content = f.read()

        if not content.startswith('---'):
            continue

        parts = content.split('---', 2)
        if len(parts) < 3:
            continue

        frontmatter = parts[1]
        body = parts[2].strip()

        # Parse frontmatter
        metadata = {}
        current_key = None
        current_value = []

        for line in frontmatter.strip().split('\n'):
            if ':' in line and not line.startswith(' '):
                if current_key:
                    metadata[current_key] = '\n'.join(current_value).strip()
                key, value = line.split(':', 1)
                current_key = key.strip()
                current_value = [value.strip()]
            elif current_key:
                current_value.append(line.strip())

        if current_key:
            metadata[current_key] = '\n'.join(current_value).strip()

        # Parse attachments (markdown link format)
        attachments_raw = metadata.get('attachments', 'None')
        attachment_list = []

        if attachments_raw != 'None' and attachments_raw != '- None':
            for line in attachments_raw.split('\n'):
                line = line.strip()
                if line.startswith('- [') and '](' in line:
                    match = re.match(r'- \[([^\]]+)\]\(([^)]+)\)', line)
                    if match:
                        filename = match.group(1)
                        att_path = match.group(2)

                        # Make path absolute if relative
                        if not os.path.isabs(att_path):
                            att_path = os.path.join(os.path.dirname(markdown_dir), att_path)

                        if os.path.exists(att_path):
                            ext = Path(att_path).suffix.lower()

                            # Skip images and email files
                            if ext not in IMAGE_EXTS and ext not in EMAIL_EXTS:
                                file_size = os.path.getsize(att_path)
                                attachment_list.append({
                                    'name': filename,
                                    'path': att_path,
                                    'size': file_size,
                                    'ext': ext
                                })

        # Normalize subject for threading
        subject = metadata.get('subject', 'Unknown')
        base_subject = re.sub(r'^(Re|Fwd|Fw):\s*', '', subject, flags=re.IGNORECASE).strip()
        base_subject = re.sub(r'\s+', ' ', base_subject).strip()

        email_data = {
            'id': len(emails),
            'from': metadata.get('from', ''),
            'to': metadata.get('to', ''),
            'cc': metadata.get('cc', ''),
            'date': metadata.get('date', ''),
            'subject': subject,
            'body': body,
            'attachments': attachment_list,
            'thread': base_subject
        }

        emails.append(email_data)
        threads[base_subject].append(email_data['id'])  # Always store IDs

    # Build thread summaries
    thread_summaries = []
    for subject, thread_items in threads.items():
        # thread_items always contains IDs
        thread_emails = [emails[i] for i in thread_items]

        participants = sorted(list(set(e['from'] for e in thread_emails if e['from'])))

        thread_summary = {
            'subject': subject,
            'count': len(thread_emails),
            'participants': participants,
            'dates': {
                'first': thread_emails[0]['date'],
                'last': thread_emails[-1]['date']
            },
            'emails': thread_items  # Always use IDs
        }

        thread_summaries.append(thread_summary)

    # Build output structure (always same structure, compressed only affects formatting)
    output = {
        'summary': {
            'emails': len(emails),
            'threads': len(threads),
            'attachments': sum(len(e['attachments']) for e in emails),
            'participants': sorted(list(set(e['from'] for e in emails if e['from'])))
        },
        'threads': sorted(thread_summaries, key=lambda x: x['count'], reverse=True),
        'emails': emails
    }

    # Write JSON
    with open(output_file, 'w', encoding='utf-8') as f:
        if compressed:
            # Minified: single line, minimal whitespace
            json.dump(output, f, ensure_ascii=False, separators=(',', ':'))
        else:
            # Pretty-printed: indented, human-readable
            json.dump(output, f, indent=2, ensure_ascii=False)

    file_size = os.path.getsize(output_file)
    print(f"   [OK] Exported {len(emails)} emails to JSON")
    print(f"   [OK] File: {output_file} ({file_size/1024:.0f}KB)")

    return len(emails)


def write_json_documentation(output_dir: str):
    """Write JSON structure and search guide documentation files."""

    json_structure = """# JSON Structure

## Schema
```json
{
  "summary": {
    "emails": number,
    "threads": number,
    "attachments": number,
    "participants": [string]
  },
  "threads": [{
    "subject": string,        // Normalized (Re:/Fwd: removed)
    "count": number,
    "participants": [string],
    "dates": {"first": string, "last": string},  // ISO 8601
    "emails": [number]        // IDs referencing emails array
  }],
  "emails": [{
    "id": number,
    "from": string,
    "to": string,
    "cc": string,
    "date": string,           // ISO 8601
    "subject": string,        // Original (includes Re:/Fwd:)
    "body": string,
    "attachments": [{
      "name": string,
      "path": string,
      "size": number,
      "ext": string
    }],
    "thread": string          // Normalized subject
  }]
}
```

## Key Points
- **No duplication**: Emails stored once, threads reference by ID
- **Pre-sorted**: Threads by count (desc), emails chronologically
- **Lookup pattern**: threads → email IDs → emails[id]
- **Images/eml files**: Excluded from attachments
"""

    search_guide = """# Search Guide

## Strategy: Top-Down Approach
1. Check `summary` for overview (counts, participants)
2. Browse `threads[]` for relevant conversations (pre-sorted by activity)
3. Look up specific emails via `emails[id]`

## Common Queries

**By sender**: Filter `emails[]` where `from` matches
**By topic**: Search `threads[].subject` first, then `emails[].body`
**By date**: Parse `date` field (ISO 8601), filter by range
**By attachments**: Filter where `attachments.length > 0`
**Full thread**: Find in `threads[]`, get IDs from `emails[]`, map to `emails[id]`
**Participants**: Use `thread.participants[]` or aggregate `from` fields
**Most active**: `threads[0]` (already sorted by count desc)

## Optimization Tips
- Use thread metadata before loading full emails
- `threads[]` sorted by count, `emails[]` by chronology
- `summary.participants` avoids scanning all emails
- Thread subjects normalized, email subjects original

## Pitfalls
- Don't confuse `thread.subject` (normalized) with `email.subject` (original)
- Check both `to` and `cc` for participants
- Images/eml excluded from attachments
"""

    # Write files
    try:
        with open(os.path.join(output_dir, 'JSON_STRUCTURE.md'), 'w', encoding='utf-8') as f:
            f.write(json_structure)

        with open(os.path.join(output_dir, 'CLAUDE_SEARCH_GUIDE.md'), 'w', encoding='utf-8') as f:
            f.write(search_guide)

        print(f"   [OK] Documentation written (JSON_STRUCTURE.md, CLAUDE_SEARCH_GUIDE.md)")
    except Exception as e:
        logger.warning(f"Failed to write documentation: {e}")


def main():
    """Main function to run the email export tool."""
    # Parse command-line arguments
    parser = argparse.ArgumentParser(description='Email Export Tool v2 (Sync + Raw + Markdown)')
    parser.add_argument('--compress-json', action='store_true',
                        help='Compress JSON export (smaller file size, emails referenced by ID)')
    args = parser.parse_args()

    print("Email Export Tool v2 (Sync + Raw + Markdown)")
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
    print("  4. Export markdown to JSON")
    choice = input("\nChoice (1/2/3/4): ").strip()
    print()

    downloaded_count = 0

    if choice == '4':
        # Export to JSON
        json_file = os.path.join(config['output_dir'], 'emails.json')
        email_count = export_to_json(markdown_dir, json_file, compressed=args.compress_json)

        # Write documentation files
        write_json_documentation(config['output_dir'])

        print("\n" + "=" * 60)
        print("JSON Export complete!")
        print("=" * 60)
        print(f"Emails exported: {email_count}")
        print(f"Output file: {json_file}")
        print(f"\nStructure:")
        print(f"  - Emails stored once in 'emails' array")
        print(f"  - Threads reference emails by ID (no duplication)")
        print(f"  - Optimized for LLM processing")
        if args.compress_json:
            print(f"\nFormatting: Minified (single line, smallest file size)")
        else:
            print(f"\nFormatting: Pretty-printed (indented, human-readable)")
            print(f"Tip: Use --compress-json flag to minify output")
        print("\nTip: Upload this JSON to Claude for conversation analysis!")
        print("=" * 60)
        return 0

    if choice in ['1', '3']:
        # Connect to server
        try:
            mail = imaplib.IMAP4_SSL(config['imap_server'], config['imap_port'])
            mail.login(config['email'], config['password'])
            print("[OK] Connected successfully.")
        except Exception as e:
            print(f"[Error] Connection failed: {e}")
            return 1

        # Sync emails (only download new ones)
        downloaded = download_emails_from_imap(
            mail, config['imap_folders'], raw_emails_dir,
            include_sent_folder=config['include_related_sent']
        )

        mail.close()
        mail.logout()

        downloaded_count = len(downloaded)
        print(f"[Download] Synced {downloaded_count} new emails\n")

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
