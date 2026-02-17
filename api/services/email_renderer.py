"""
Email Template Renderer Service using Jinja2.

This service handles rendering of HTML email templates with support for:
- Multiple locales (i18n-ready)
- Automatic plain text generation from HTML
- Template inheritance and composition
"""

from jinja2 import Environment, FileSystemLoader, TemplateNotFound, ChoiceLoader
from typing import Dict, Tuple
import os
import re
import logging

logger = logging.getLogger(__name__)


class EmailRenderer:
    """
    Renders email templates using Jinja2.

    Templates are organized by locale:
    - templates/emails/es/  (Spanish)
    - templates/emails/en/  (English - future)
    """

    def __init__(self):
        """Initialize Jinja2 environment with email templates directory."""
        # Get templates directory relative to this file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        emails_dir = os.path.join(current_dir, '..', 'templates', 'emails')
        es_dir = os.path.join(emails_dir, 'es')

        # Ensure template directories exist
        if not os.path.exists(emails_dir):
            raise RuntimeError(f"Email templates directory not found: {emails_dir}")

        # Initialize Jinja2 environment with ChoiceLoader to search in both directories
        # This allows templates in es/ to reference base.html which is also in es/
        self.env = Environment(
            loader=ChoiceLoader([
                FileSystemLoader(es_dir),  # Check locale-specific directory first
                FileSystemLoader(emails_dir),  # Then check parent directory
            ]),
            autoescape=True,  # Auto-escape HTML for security
            trim_blocks=True,
            lstrip_blocks=True
        )

        logger.info(f"EmailRenderer initialized with template directories: {es_dir} and {emails_dir}")

    def render(
        self,
        template_name: str,
        context: Dict[str, any],
        locale: str = 'es'
    ) -> Tuple[str, str]:
        """
        Render an email template to HTML and plain text.

        Args:
            template_name: Name of the template file (e.g., 'password_reset_worker.html')
            context: Dictionary of variables to pass to the template
            locale: Language code for template selection (default: 'es')

        Returns:
            Tuple of (html_body, text_body)

        Raises:
            TemplateNotFound: If the template doesn't exist
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
                locale='es'
            )
        """
        try:
            # Template name without locale prefix (ChoiceLoader will find it in es/ first)
            logger.info(f"Rendering email template: {template_name}")
            logger.debug(f"Template context: {list(context.keys())}")

            # Load and render template
            template = self.env.get_template(template_name)
            html_body = template.render(**context)

            # Generate plain text version
            text_body = self._html_to_text(html_body)

            logger.info(f"Successfully rendered template: {template_name}")
            return html_body, text_body

        except TemplateNotFound as e:
            logger.error(f"Template not found: {template_name}")
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

    def list_templates(self, locale: str = 'es') -> list:
        """
        List available templates for a given locale.

        Args:
            locale: Language code

        Returns:
            List of template filenames
        """
        template_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..',
            'templates',
            'emails',
            locale
        )

        if not os.path.exists(template_dir):
            return []

        templates = [
            f for f in os.listdir(template_dir)
            if f.endswith('.html') and f != 'base.html'
        ]

        return sorted(templates)


# Singleton instance
email_renderer = EmailRenderer()
