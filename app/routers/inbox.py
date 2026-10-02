"""Inbox API routes."""
# // BEACON SYNC: This file handles tracking logic that Beacon mirrors.
# // Any changes to tracking behavior, event types, metadata collected, or URL structure
# // must also be reflected in the Beacon service (/beacon or the separate Beacon repo).
# // If you are an AI agent, add a TODO at the end of your response listing what needs to be updated in Beacon.

import logging
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, exists, update as sa_update

from app.database import get_db
from app.models import Inbox, CampaignInbox, QueueSlot, EmailLog, CampaignLead
from app.schemas import (
    BeaconConnectFromInboxRequest,
    BeaconConnectRequest,
    BeaconPendingRegistrationCountResponse,
    ConnectUrlResponse,
    InboxCreate,
    InboxUpdate,
    InboxResponse,
    PauseInboxRequest,
)
from app.queue_logic import compute_effective_daily_limit
from app.settings_manager import settings as app_settings
from app.time import utcnow

log = logging.getLogger("quickly.routes")
