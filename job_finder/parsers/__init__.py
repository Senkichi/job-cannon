"""Email parsers for different job board alert formats."""

from job_finder.parsers.linkedin_parser import parse_linkedin_alert
from job_finder.parsers.glassdoor_parser import parse_glassdoor_alert
from job_finder.parsers.indeed_parser import parse_indeed_alert
from job_finder.parsers.ziprecruiter_parser import parse_ziprecruiter_alert

__all__ = [
    "parse_linkedin_alert",
    "parse_glassdoor_alert",
    "parse_indeed_alert",
    "parse_ziprecruiter_alert",
]
