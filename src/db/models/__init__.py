"""Modeles SQLAlchemy du hub.

IMPORTANT : tous les modeles doivent etre importes ici pour qu'Alembic les
detecte au moment du `revision --autogenerate`.
"""

from src.db.base import Base
from src.db.models.account import Account
from src.db.models.browser_history import BrowserHistory
from src.db.models.calendar_event import CalendarEvent
from src.db.models.contact import Contact
from src.db.models.credit_card_transaction import CreditCardTransaction
from src.db.models.drive_file import DriveFile
from src.db.models.email import Email
from src.db.models.health_metric import HealthMetric
from src.db.models.investment_position import InvestmentPosition
from src.db.models.investment_transaction import InvestmentTransaction
from src.db.models.location_address import LocationAddress
from src.db.models.location_point import LocationPoint
from src.db.models.location_visit import LocationActivity, LocationVisit
from src.db.models.named_place import NamedPlace, TripNote
from src.db.models.news_article import NewsArticle
from src.db.models.oauth_token import OAuthToken
from src.db.models.photo import Photo
from src.db.models.photo_embedding import PhotoEmbedding
from src.db.models.photo_face import FaceCluster, PhotoFace
from src.db.models.push_subscription import PushSubscription
from src.db.models.removal_request import RemovalRequest
from src.db.models.social_post import SocialPost
from src.db.models.streaming_activity import StreamingActivity
from src.db.models.task import Task
from src.db.models.transaction import Transaction
from src.db.models.youtube_activity import YouTubeActivity

__all__ = [
    "Base",
    "Account",
    "BrowserHistory",
    "Transaction",
    "CreditCardTransaction",
    "InvestmentTransaction",
    "InvestmentPosition",
    "LocationAddress",
    "LocationPoint",
    "LocationVisit",
    "LocationActivity",
    "NamedPlace",
    "TripNote",
    "OAuthToken",
    "Email",
    "CalendarEvent",
    "HealthMetric",
    "Photo",
    "DriveFile",
    "Contact",
    "Task",
    "YouTubeActivity",
    "NewsArticle",
    "PhotoEmbedding",
    "PhotoFace",
    "FaceCluster",
    "PushSubscription",
    "RemovalRequest",
    "SocialPost",
    "StreamingActivity",
]
