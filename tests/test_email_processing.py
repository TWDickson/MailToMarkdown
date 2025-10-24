"""
Tests for email processing functions in MailToMarkdown.

Tests the core utility functions that are used throughout the application.
"""

import pytest

from export_mails import (
    EmailPatterns,
    sanitize_filename,
    clean_header_field,
    convert_urls_to_markdown,
    clean_email_body,
    detect_reply_separator,
    is_contentless_forward,
    MIN_CONTENT_LENGTH,
    MAX_SIGNATURE_LENGTH,
)


# Fixtures
@pytest.fixture(autouse=True)
def initialize_patterns():
    """Initialize email patterns before each test."""
    EmailPatterns._initialize_patterns()


# EmailPatterns Tests
class TestEmailPatterns:
    """Test the EmailPatterns class for pattern detection."""

    def test_detect_reply_separator_gmail_format(self):
        """Test Gmail-style reply separator detection."""
        body = "On Tue, Oct 21, 2025 at 3:45 PM John Doe <john@example.com> wrote:\nQuoted text"
        result = EmailPatterns.detect_reply_separator(body)
        assert result == 0

    def test_detect_reply_separator_simple_format(self):
        """Test simple 'On date, Name wrote:' format."""
        body = "On Monday, October 21, 2025, John wrote:\nQuoted text"
        result = EmailPatterns.detect_reply_separator(body)
        assert result == 0

    def test_detect_reply_separator_outlook_format(self):
        """Test Outlook-style From:/Sent: headers."""
        body = "From: John Doe\nSent: Monday\nQuoted text"
        result = EmailPatterns.detect_reply_separator(body)
        assert result == 0

    def test_detect_reply_separator_forward(self):
        """Test forward message detection."""
        body = "---------- Forwarded message ---------\nFrom: John"
        result = EmailPatterns.detect_reply_separator(body)
        assert result == 0

    def test_detect_reply_separator_quote_marks(self):
        """Test multiple consecutive > quote marks."""
        body = "Original content\n> Quoted line 1\n> Quoted line 2\n> Quoted line 3"
        result = EmailPatterns.detect_reply_separator(body)
        assert result == 1

    def test_detect_reply_separator_none(self):
        """Test that regular content returns None."""
        body = "This is just regular email content without any reply separator."
        result = EmailPatterns.detect_reply_separator(body)
        assert result is None

    def test_is_forward_subject_fwd(self):
        """Test forward subject detection with 'Fwd:'."""
        assert EmailPatterns.is_forward_subject("Fwd: Important message")
        assert EmailPatterns.is_forward_subject("FW: Meeting notes")
        assert EmailPatterns.is_forward_subject("[Fwd: Info]")

    def test_is_forward_subject_not_forward(self):
        """Test that regular subjects return False."""
        assert not EmailPatterns.is_forward_subject("Regular subject")
        assert not EmailPatterns.is_forward_subject("Re: Reply to email")

    def test_is_signature_line(self):
        """Test signature line detection."""
        assert EmailPatterns.is_signature_line("- John Doe")
        assert EmailPatterns.is_signature_line("Best regards")
        assert EmailPatterns.is_signature_line("Thanks")
        assert EmailPatterns.is_signature_line("Sent from my iPhone")

    def test_is_not_signature_line(self):
        """Test that regular content is not detected as signature."""
        assert not EmailPatterns.is_signature_line("This is regular content.")
        assert not EmailPatterns.is_signature_line("What do you think?")


# Filename Sanitization Tests
class TestFilenameSanitization:
    """Test filename sanitization function."""

    def test_sanitize_filename_basic(self):
        """Test basic filename sanitization."""
        assert sanitize_filename("Test File") == "Test File"  # Spaces preserved
        assert sanitize_filename("file.txt") == "file.txt"

    def test_sanitize_filename_invalid_chars(self):
        """Test removal of invalid characters."""
        assert sanitize_filename("test/file:name") == "test_file_name"  # Invalid chars become underscores
        assert sanitize_filename("file<name>here") == "file_name_here"

    def test_sanitize_filename_newlines(self):
        """Test handling of newlines."""
        assert sanitize_filename("test\nfile") == "testfile"  # Newlines removed
        assert sanitize_filename("test\r\nfile") == "testfile"

    def test_sanitize_filename_strips_whitespace(self):
        """Test whitespace trimming."""
        assert sanitize_filename("  test  ") == "test"


# Header Cleaning Tests
class TestHeaderCleaning:
    """Test email header field cleaning."""

    def test_clean_header_field_basic(self):
        """Test basic header cleaning."""
        assert clean_header_field("Test Header") == "Test Header"
        assert clean_header_field("Simple text") == "Simple text"

    def test_clean_header_field_newlines(self):
        """Test newline removal."""
        assert clean_header_field("Header\nValue") == "Header Value"
        assert clean_header_field("Multi\n\nLine") == "Multi Line"

    def test_clean_header_field_multiple_spaces(self):
        """Test multiple space collapsing."""
        assert clean_header_field("Too    many    spaces") == "Too many spaces"

    def test_clean_header_field_empty(self):
        """Test empty input."""
        assert clean_header_field("") == ""
        assert clean_header_field(None) == ""


# URL Conversion Tests
class TestURLConversion:
    """Test URL to markdown conversion."""

    def test_convert_plain_url(self):
        """Test converting plain URLs."""
        result = convert_urls_to_markdown("Visit http://example.com for more")
        assert "[http://example.com](http://example.com)" in result  # Converts to markdown link

    def test_convert_url_in_brackets(self):
        """Test URL already in angle brackets."""
        text = "Visit <http://example.com> for more"
        result = convert_urls_to_markdown(text)
        # Angle brackets are also converted to markdown links
        assert "[http://example.com](http://example.com)" in result

    def test_convert_mailto_removal(self):
        """Test mailto: link handling."""
        text = "[Email me](mailto:test@example.com)"
        result = convert_urls_to_markdown(text)
        # Mailto links are preserved in markdown
        assert result == text

    def test_dont_convert_existing_markdown(self):
        """Test that existing markdown links are preserved."""
        text = "Check [this link](http://example.com)"
        result = convert_urls_to_markdown(text)
        assert result == text


# Email Body Cleaning Tests
class TestEmailBodyCleaning:
    """Test email body cleaning and normalization."""

    def test_clean_email_body_trailing_whitespace(self):
        """Test trailing whitespace removal."""
        result = clean_email_body("Test content   \n")
        assert result == "Test content"

    def test_clean_email_body_multiple_newlines(self):
        """Test multiple newline collapsing."""
        result = clean_email_body("Line1\n\n\n\nLine2")
        assert "\n\n\n\n" not in result

    def test_clean_email_body_strips_ends(self):
        """Test stripping of leading/trailing whitespace."""
        result = clean_email_body("  \n  Content  \n  ")
        assert result.strip() == result


# Contentless Forward Tests
class TestContentlessForward:
    """Test detection of forwarded emails without original content."""

    def test_is_contentless_forward_true(self):
        """Test detection of contentless forward."""
        metadata = {"subject": "Fwd: Important"}
        body = "---------- Forwarded message ---------\nFrom: John\nOriginal content"
        result = is_contentless_forward(metadata, body)
        assert result is True

    def test_is_contentless_forward_with_content(self):
        """Test forward with added content."""
        metadata = {"subject": "Fwd: Info"}
        body = "Here's the info you requested:\n\n---------- Forwarded message ---------\nOriginal"
        result = is_contentless_forward(metadata, body)
        assert result is False

    def test_is_not_forward(self):
        """Test non-forward email."""
        metadata = {"subject": "Regular Subject"}
        body = "Regular email content"
        result = is_contentless_forward(metadata, body)
        assert result is False


# Reply Separator Detection Tests
class TestReplySeparatorDetection:
    """Test standalone reply separator detection function (delegates to EmailPatterns)."""

    def test_detect_reply_separator_delegates_to_class(self):
        """Test that standalone function delegates to EmailPatterns class method."""
        body = "On Monday, October 21, 2025, John wrote:\n> Original message"
        # Both should return the same result
        standalone_result = detect_reply_separator(body)
        class_result = EmailPatterns.detect_reply_separator(body)
        assert standalone_result == class_result
        assert standalone_result == 0  # Should detect separator at line 0
