"""Ringshift creator outreach: discovery, drafts, human approval, Gmail delivery.

Separate from the X autopilot (scripts/, tests/, autopilot.yml): own workflow, own state branch, own secrets.
Only `outreach.mailer` can talk to Gmail, and only the manually dispatched `mail` job of outreach.yml runs it.
See outreach/README.md.
"""
