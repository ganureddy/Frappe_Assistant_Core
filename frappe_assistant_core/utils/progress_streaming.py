# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2025 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Progress Streaming System for Frappe Assistant Core
Provides real-time progress updates for long-running operations
"""

import json
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

import frappe

from frappe_assistant_core.utils.cache import get_cached_server_settings
from frappe_assistant_core.utils.logger import api_logger


class ProgressStatus(Enum):
    STARTED = "started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ProgressUpdate:
    """Represents a progress update for an operation"""

    operation_id: str
    user: str
    operation_type: str
    status: ProgressStatus
    progress_percent: int = 0
    current_step: str = ""
    total_steps: int = 1
    current_step_number: int = 1
    message: str = ""
    error: Optional[str] = None
    metadata: Dict[str, Any] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
        if self.metadata is None:
            self.metadata = {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        data = asdict(self)
        data["status"] = self.status.value
        data["timestamp"] = self.timestamp.isoformat()
        return data


class ProgressTracker:
    """Tracks progress for individual operations"""

    def __init__(self, operation_id: str, user: str, operation_type: str):
        self.operation_id = operation_id
        self.user = user
        self.operation_type = operation_type
        self.start_time = datetime.now()
        self.last_update = self.start_time
        self.updates: List[ProgressUpdate] = []
        self._callbacks: List[Callable] = []
        self.cancelled = False

    def add_callback(self, callback: Callable[[ProgressUpdate], None]):
        """Add a callback for progress updates"""
        self._callbacks.append(callback)

    def update_progress(
        self,
        status: ProgressStatus,
        progress_percent: int = None,
        current_step: str = "",
        total_steps: int = None,
        current_step_number: int = None,
        message: str = "",
        error: str = None,
        metadata: Dict[str, Any] = None,
    ):
        """Update progress with new information"""

        if self.cancelled and status not in [ProgressStatus.CANCELLED, ProgressStatus.FAILED]:
            return  # Don't update if cancelled

        # Get previous update for defaults
        prev_update = self.updates[-1] if self.updates else None

        update = ProgressUpdate(
            operation_id=self.operation_id,
            user=self.user,
            operation_type=self.operation_type,
            status=status,
            progress_percent=progress_percent
            if progress_percent is not None
            else (prev_update.progress_percent if prev_update else 0),
            current_step=current_step or (prev_update.current_step if prev_update else ""),
            total_steps=total_steps
            if total_steps is not None
            else (prev_update.total_steps if prev_update else 1),
            current_step_number=current_step_number
            if current_step_number is not None
            else (prev_update.current_step_number if prev_update else 1),
            message=message,
            error=error,
            metadata=metadata or {},
        )

        self.updates.append(update)
        self.last_update = update.timestamp

        # Store in cache for real-time access
        self._cache_update(update)

        # Notify callbacks
        self._notify_callbacks(update)

        # Log important updates
        if status in [ProgressStatus.STARTED, ProgressStatus.COMPLETED, ProgressStatus.FAILED]:
            api_logger.info(f"Operation {self.operation_id}: {status.value} - {message}")

    def _cache_update(self, update: ProgressUpdate):
        """Cache the update for real-time access"""
        try:
            cache_key = f"progress_{self.operation_id}"
            frappe.cache().set_value(cache_key, update.to_dict(), expires_in_sec=3600)

            # Also maintain a user-specific list of active operations
            user_ops_key = f"user_operations_{self.user}"
            user_ops = frappe.cache().get_value(user_ops_key) or []

            # Update or add operation
            found = False
            for i, op in enumerate(user_ops):
                if op.get("operation_id") == self.operation_id:
                    user_ops[i] = update.to_dict()
                    found = True
                    break

            if not found:
                user_ops.append(update.to_dict())

            # Keep only recent operations (last 50)
            user_ops = user_ops[-50:]
            frappe.cache().set_value(user_ops_key, user_ops, expires_in_sec=7200)

        except Exception as e:
            api_logger.error(f"Failed to cache progress update: {str(e)}")

    def _notify_callbacks(self, update: ProgressUpdate):
        """Notify all registered callbacks"""
        for callback in self._callbacks:
            try:
                callback(update)
            except Exception as e:
                api_logger.error(f"Progress callback error: {str(e)}")

    def cancel(self):
        """Cancel the operation"""
        self.cancelled = True
        self.update_progress(status=ProgressStatus.CANCELLED, message="Operation cancelled by user")

    def get_latest_update(self) -> Optional[ProgressUpdate]:
        """Get the latest progress update"""
        return self.updates[-1] if self.updates else None

    def get_duration(self) -> float:
        """Get operation duration in seconds"""
        return (self.last_update - self.start_time).total_seconds()


class ProgressStreamingService:
    """Centralized service for managing progress streaming"""

    def __init__(self):
        self.active_trackers: Dict[str, ProgressTracker] = {}
        self._lock = threading.Lock()
        self.websocket_callbacks: List[Callable] = []

    def create_tracker(self, operation_id: str, user: str, operation_type: str) -> ProgressTracker:
        """Create a new progress tracker"""
        with self._lock:
            tracker = ProgressTracker(operation_id, user, operation_type)
            self.active_trackers[operation_id] = tracker

            # Add WebSocket callback if available
            tracker.add_callback(self._websocket_broadcast)

            # Start tracking
            tracker.update_progress(status=ProgressStatus.STARTED, message=f"Started {operation_type}")

            return tracker

    def get_tracker(self, operation_id: str) -> Optional[ProgressTracker]:
        """Get an existing progress tracker"""
        with self._lock:
            return self.active_trackers.get(operation_id)

    def remove_tracker(self, operation_id: str):
        """Remove a completed tracker"""
        with self._lock:
            if operation_id in self.active_trackers:
                del self.active_trackers[operation_id]

    def cancel_operation(self, operation_id: str, user: str = None) -> bool:
        """Cancel an operation"""
        with self._lock:
            tracker = self.active_trackers.get(operation_id)
            if not tracker:
                return False

            # Check user permission
            if user and tracker.user != user and not frappe.has_permission("System Manager"):
                return False

            tracker.cancel()
            return True

    def get_user_operations(self, user: str) -> List[Dict[str, Any]]:
        """Get active operations for a user"""
        try:
            cache_key = f"user_operations_{user}"
            operations = frappe.cache().get_value(cache_key) or []

            # Filter out completed operations older than 1 hour
            current_time = datetime.now()
            filtered_ops = []

            for op in operations:
                try:
                    op_time = datetime.fromisoformat(op["timestamp"])
                    age_minutes = (current_time - op_time).total_seconds() / 60

                    # Keep if not completed or completed within last hour
                    if op["status"] not in ["completed", "failed", "cancelled"] or age_minutes < 60:
                        filtered_ops.append(op)
                except Exception:
                    continue  # Skip invalid entries

            return filtered_ops

        except Exception as e:
            api_logger.error(f"Failed to get user operations: {str(e)}")
            return []

    def add_websocket_callback(self, callback: Callable[[ProgressUpdate], None]):
        """Add a WebSocket broadcast callback"""
        self.websocket_callbacks.append(callback)

    def _websocket_broadcast(self, update: ProgressUpdate):
        """Broadcast update to WebSocket connections"""
        for callback in self.websocket_callbacks:
            try:
                callback(update)
            except Exception as e:
                api_logger.error(f"WebSocket broadcast error: {str(e)}")

    def cleanup_old_trackers(self, max_age_hours: int = 2):
        """Clean up old completed trackers"""
        current_time = datetime.now()
        expired_trackers = []

        with self._lock:
            for operation_id, tracker in self.active_trackers.items():
                age_hours = (current_time - tracker.last_update).total_seconds() / 3600
                latest_update = tracker.get_latest_update()

                if (
                    latest_update
                    and latest_update.status
                    in [ProgressStatus.COMPLETED, ProgressStatus.FAILED, ProgressStatus.CANCELLED]
                    and age_hours > max_age_hours
                ):
                    expired_trackers.append(operation_id)

            for operation_id in expired_trackers:
                del self.active_trackers[operation_id]

        api_logger.info(f"Cleaned up {len(expired_trackers)} old progress trackers")
        return len(expired_trackers)


# Global progress streaming service
_progress_service: Optional[ProgressStreamingService] = None


def get_progress_service() -> ProgressStreamingService:
    """Get or create the global progress streaming service"""
    global _progress_service
    if _progress_service is None:
        _progress_service = ProgressStreamingService()
    return _progress_service


class ProgressContext:
    """Context manager for progress tracking"""

    def __init__(self, operation_type: str, user: str = None, operation_id: str = None):
        self.operation_type = operation_type
        self.user = user or frappe.session.user
        self.operation_id = operation_id or frappe.generate_hash(length=8)
        self.tracker = None

    def __enter__(self) -> ProgressTracker:
        service = get_progress_service()
        self.tracker = service.create_tracker(self.operation_id, self.user, self.operation_type)
        return self.tracker

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.tracker:
            if exc_type:
                self.tracker.update_progress(
                    status=ProgressStatus.FAILED,
                    progress_percent=100,
                    message=f"Operation failed: {str(exc_val)}",
                    error=str(exc_val),
                )
            else:
                latest = self.tracker.get_latest_update()
                if latest and latest.status == ProgressStatus.RUNNING:
                    self.tracker.update_progress(
                        status=ProgressStatus.COMPLETED,
                        progress_percent=100,
                        message="Operation completed successfully",
                    )

            # Remove tracker after completion
            service = get_progress_service()
            service.remove_tracker(self.operation_id)


# API Endpoints for progress streaming


@frappe.whitelist()
def get_operation_progress(operation_id: str) -> Dict[str, Any]:
    """Get progress for a specific operation"""
    try:
        cache_key = f"progress_{operation_id}"
        progress_data = frappe.cache().get_value(cache_key)

        if not progress_data:
            return {"success": False, "message": "Operation not found"}

        return {"success": True, "progress": progress_data}

    except Exception as e:
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def get_user_operations() -> Dict[str, Any]:
    """Get all operations for the current user"""
    try:
        service = get_progress_service()
        operations = service.get_user_operations(frappe.session.user)

        return {"success": True, "operations": operations}

    except Exception as e:
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def cancel_operation(operation_id: str) -> Dict[str, Any]:
    """Cancel a running operation"""
    try:
        service = get_progress_service()
        cancelled = service.cancel_operation(operation_id, frappe.session.user)

        return {
            "success": cancelled,
            "message": "Operation cancelled" if cancelled else "Operation could not be cancelled",
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


# Decorator for automatic progress tracking
def track_progress(operation_type: str):
    """Decorator to automatically track progress for functions"""

    def decorator(func):
        def wrapper(*args, **kwargs):
            with ProgressContext(operation_type) as tracker:
                # Store tracker in thread-local storage for access within function
                import threading

                threading.current_thread().progress_tracker = tracker

                try:
                    result = func(*args, **kwargs)
                    return result
                finally:
                    if hasattr(threading.current_thread(), "progress_tracker"):
                        delattr(threading.current_thread(), "progress_tracker")

        return wrapper

    return decorator


def get_current_progress_tracker() -> Optional[ProgressTracker]:
    """Get the current thread's progress tracker"""
    import threading

    return getattr(threading.current_thread(), "progress_tracker", None)


def update_progress(progress_percent: int = None, message: str = "", current_step: str = "", **kwargs):
    """Convenience function to update current operation progress"""
    tracker = get_current_progress_tracker()
    if tracker:
        tracker.update_progress(
            status=ProgressStatus.RUNNING,
            progress_percent=progress_percent,
            message=message,
            current_step=current_step,
            **kwargs,
        )
