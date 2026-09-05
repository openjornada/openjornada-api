"""
Email Template Renderer Service using Jinja2.

This service handles rendering of HTML email templates with support for:
- Multiple locales with per-locale template directories
  (templates/emails/<locale>/) and fallback to 'es'
- Automatic plain text generation from HTML
- Template inheritance and composition
"""

from jinja2 import Environment, FileSystemLoader, TemplateNotFound, ChoiceLoader
from typing import Dict, List, Optional, Tuple
import os
import re
import logging

from ..models.i18n import DEFAULT_LOCALE

logger = logging.getLogger(__name__)


class EmailRenderer:
    """
    Renders email templates using Jinja2.

    Templates are organized by locale:
    - templates/emails/es/  (Spanish — global fallback)
    - templates/emails/en/  (English)
    - templates/emails/ca/  (Catalan)

    For a requested locale, the search path is
    ``[<locale>/, es/, emails/]``: the locale directory first, then the
    Spanish fallback, then the shared root. A Jinja2 ``Environment`` is built
    (and cached) per locale, so a missing locale template — or a ``base.html``
    a locale template extends — transparently falls back to ``es``.
    """

    def __init__(self):
        """Initialize paths to the email templates directory."""
        # Get templates directory relative to this file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self._emails_dir = os.path.join(current_dir, '..', 'templates', 'emails')

        # Ensure template directory exists
        if not os.path.exists(self._emails_dir):
            raise RuntimeError(f"Email templates directory not found: {self._emails_dir}")

        # One Jinja2 Environment per locale (built lazily, then cached) to
        # avoid rebuilding loaders/template caches on every send.
        self._envs: Dict[str, Environment] = {}

        logger.info(f"EmailRenderer initialized with templates directory: {self._emails_dir}")

    def _get_env(self, locale: Optional[str]) -> Environment:
        """Return (creating and caching if needed) the Environment for locale."""
        locale = locale or DEFAULT_LOCALE
        env = self._envs.get(locale)
        if env is None:
            env = Environment(
                loader=ChoiceLoader([
                    FileSystemLoader(os.path.join(self._emails_dir, locale)),  # requested locale first
                    FileSystemLoader(os.path.join(self._emails_dir, DEFAULT_LOCALE)),  # then es fallback
                    FileSystemLoader(self._emails_dir),  # then shared root
                ]),
                autoescape=True,  # Auto-escape HTML for security
                trim_blocks=True,
                lstrip_blocks=True
            )
            self._envs[locale] = env
            logger.info(f"EmailRenderer created environment for locale: {locale}")
        return env

    def render(
        self,
        template_name: str,
        context: Dict[str, any],
        locale: str = DEFAULT_LOCALE
    ) -> Tuple[str, str]:
        """
        Render an email template to HTML and plain text.

        Args:
            template_name: Name of the template file (e.g., 'password_reset_worker.html')
            context: Dictionary of variables to pass to the template
            locale: Language code for template selection (default: 'es').
                    Falls back to 'es' when the locale template is missing.

        Returns:
            Tuple of (html_body, text_body)

        Raises:
            TemplateNotFound: If the template doesn't exist in the locale nor
                              in the fallback chain
            TemplateSyntaxError: If there's a syntax error in the template

        Example:
            renderer = EmailRenderer()
            html, text = renderer.render(
                'password_reset_worker.html',
                {
                    'app_name': 'OpenJornada',
                    'worker_name': 'Juan',
                    'reset_link': 'https://...',
                    'contact_email': 'support@example.com'
                },
                locale='en'
            )
        """
        try:
            # The per-locale ChoiceLoader resolves the template (and any
            # base.html it extends) with fallback: <locale>/ -> es/ -> emails/
            logger.info(f"Rendering email template: {template_name} (locale={locale})")
            logger.debug(f"Template context: {list(context.keys())}")

            # Load and render template
            template = self._get_env(locale).get_template(template_name)
            html_body = template.render(**context)

            # Generate plain text version
            text_body = self._html_to_text(html_body)

            logger.info(f"Successfully rendered template: {template_name}")
            return html_body, text_body

        except TemplateNotFound:
            logger.error(f"Template not found: {template_name} (locale={locale})")
            raise
        except Exception as e:
            logger.error(f"Error rendering template {template_name}: {e}")
            raise

    def _html_to_text(self, html: str) -> str:
        """
        Convert HTML to plain text.

        This is a simple implementation that:
        - Removes HTML tags
        - Converts common entities
        - Preserves basic structure

        For production, consider using libraries like html2text or beautifulsoup.

        Args:
            html: HTML string to convert

        Returns:
            Plain text version
        """
        # Remove HTML comments
        text = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)

        # Replace <br> and <p> with newlines
        text = re.sub(r'<br\s*/?>', '\n', text)
        text = re.sub(r'</p>', '\n\n', text)
        text = re.sub(r'<p[^>]*>', '', text)

        # Replace links with text + URL
        text = re.sub(
            r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            r'\2 (\1)',
            text
        )

        # Remove all other HTML tags
        text = re.sub(r'<[^>]+>', '', text)

        # Decode common HTML entities
        text = text.replace('&nbsp;', ' ')
        text = text.replace('&amp;', '&')
        text = text.replace('&lt;', '<')
        text = text.replace('&gt;', '>')
        text = text.replace('&quot;', '"')

        # Clean up whitespace
        # Replace multiple spaces with single space
        text = re.sub(r' +', ' ', text)
        # Replace multiple newlines with maximum 2
        text = re.sub(r'\n{3,}', '\n\n', text)
        # Remove leading/trailing whitespace from each line
        lines = [line.strip() for line in text.split('\n')]
        text = '\n'.join(lines)

        return text.strip()

    def list_templates(self, locale: str = DEFAULT_LOCALE) -> List[str]:
        """
        List available templates for a given locale.

        Args:
            locale: Language code

        Returns:
            List of template filenames
        """
        template_dir = os.path.join(self._emails_dir, locale)

        if not os.path.exists(template_dir):
            return []

        templates = [
            f for f in os.listdir(template_dir)
            if f.endswith('.html') and f != 'base.html'
        ]

        return sorted(templates)


# Singleton instance
email_renderer = EmailRenderer()
