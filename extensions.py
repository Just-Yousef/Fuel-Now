"""Shared extension instances (kept in their own module to avoid import cycles)."""
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"
# Where a logged-in user lands if they hit a page their role cannot use.
login_manager.unauthorized_handler_redirect = "auth.login"
