#!/usr/bin/env python3
"""
Complete API user management CLI
Usage: python -m api.manage_api_users --help
"""

import asyncio
import sys
import os
import secrets
from datetime import datetime, timedelta
from getpass import getpass
import argparse
from typing import Optional, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.database import db, client
from api.auth.auth_handler import get_password_hash
from api.services.email_service import EmailService
from bson import ObjectId


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def print_success(message: str):
    print(f"{Colors.GREEN}✓ {message}{Colors.RESET}")


def print_error(message: str):
    print(f"{Colors.RED}✗ {message}{Colors.RESET}")


def print_info(message: str):
    print(f"{Colors.BLUE}ℹ {message}{Colors.RESET}")


def print_warning(message: str):
    print(f"{Colors.YELLOW}⚠ {message}{Colors.RESET}")


async def delete_user(username: str) -> bool:
    """Delete a user by username"""
    user = await db.APIUsers.find_one({"username": username})
    if not user:
        print_error(f"User '{username}' not found")
        return False
    
    # Confirm deletion
    print_warning(f"About to delete user: {username} ({user['email']})")
    confirm = input("Are you sure? (yes/N): ").strip().lower()
    
    if confirm != 'yes':
        print_info("Deletion cancelled")
        return False
    
    result = await db.APIUsers.delete_one({"username": username})
    if result.deleted_count > 0:
        print_success(f"User '{username}' deleted successfully")
        return True
    else:
        print_error("Failed to delete user")
        return False


async def update_user_role(username: str, new_role: str) -> bool:
    """Update user role"""
    user = await db.APIUsers.find_one({"username": username})
    if not user:
        print_error(f"User '{username}' not found")
        return False
    
    if user['role'] == new_role:
        print_info(f"User already has role '{new_role}'")
        return True
    
    result = await db.APIUsers.update_one(
        {"username": username},
        {"$set": {"role": new_role}}
    )
    
    if result.modified_count > 0:
        print_success(f"User '{username}' role updated to '{new_role}'")
        return True
    else:
        print_error("Failed to update user role")
        return False


async def reset_password(username: str) -> bool:
    """Reset user password"""
    user = await db.APIUsers.find_one({"username": username})
    if not user:
        print_error(f"User '{username}' not found")
        return False
    
    # Get new password
    while True:
        password = getpass("New password (min 6 characters): ")
        if len(password) < 6:
            print_error("Password must be at least 6 characters long")
            continue
        
        password_confirm = getpass("Confirm new password: ")
        if password != password_confirm:
            print_error("Passwords do not match")
            continue
        break
    
    # Update password
    hashed_password = get_password_hash(password)
    result = await db.APIUsers.update_one(
        {"username": username},
        {"$set": {"hashed_password": hashed_password}}
    )
    
    if result.modified_count > 0:
        print_success(f"Password reset successfully for user '{username}'")
        return True
    else:
        print_error("Failed to reset password")
        return False


async def toggle_user_status(username: str) -> bool:
    """Enable/disable user"""
    user = await db.APIUsers.find_one({"username": username})
    if not user:
        print_error(f"User '{username}' not found")
        return False
    
    current_status = user.get('is_active', True)
    new_status = not current_status
    
    result = await db.APIUsers.update_one(
        {"username": username},
        {"$set": {"is_active": new_status}}
    )
    
    if result.modified_count > 0:
        status_text = "enabled" if new_status else "disabled"
        print_success(f"User '{username}' {status_text}")
        return True
    else:
        print_error("Failed to update user status")
        return False


async def show_user_details(username: str):
    """Show detailed information about a user"""
    user = await db.APIUsers.find_one({"username": username})
    if not user:
        print_error(f"User '{username}' not found")
        return
    
    print(f"\n{Colors.BOLD}User Details:{Colors.RESET}")
    print(f"  ID: {user['_id']}")
    print(f"  Username: {user['username']}")
    print(f"  Email: {user['email']}")
    print(f"  Role: {user['role']}")
    print(f"  Active: {'Yes' if user.get('is_active', True) else 'No'}")
    
    created = user.get('created_at', 'Unknown')
    if isinstance(created, datetime):
        created = created.strftime('%Y-%m-%d %H:%M:%S')
    print(f"  Created: {created}")


async def main():
    parser = argparse.ArgumentParser(
        description='Manage API users for the Time Tracking system',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Create user
    create_parser = subparsers.add_parser('create', help='Create a new user')
    create_parser.add_argument('username', help='Username')
    create_parser.add_argument('email', help='Email address')
    create_parser.add_argument('role', choices=['admin', 'tracker'], help='User role')
    create_parser.add_argument('--password', '-p', help='Password (if not provided, will prompt interactively)')
    create_parser.add_argument('--send-welcome-email', '-w', action='store_true',
                               help='Send welcome email with password reset link (only for admin users)')
    
    # Delete user
    delete_parser = subparsers.add_parser('delete', help='Delete a user')
    delete_parser.add_argument('username', help='Username to delete')
    
    # Update role
    role_parser = subparsers.add_parser('role', help='Update user role')
    role_parser.add_argument('username', help='Username')
    role_parser.add_argument('new_role', choices=['admin', 'tracker'], help='New role')
    
    # Reset password
    password_parser = subparsers.add_parser('password', help='Reset user password')
    password_parser.add_argument('username', help='Username')
    
    # Toggle status
    status_parser = subparsers.add_parser('toggle', help='Enable/disable user')
    status_parser.add_argument('username', help='Username')
    
    # Show user
    show_parser = subparsers.add_parser('show', help='Show user details')
    show_parser.add_argument('username', help='Username')
    
    # List users
    list_parser = subparsers.add_parser('list', help='List all users')

    # Send welcome email
    welcome_parser = subparsers.add_parser('welcome', help='Send welcome email to existing admin user')
    welcome_parser.add_argument('email', help='Email of the admin user')

    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        if args.command == 'create':
            # Check if user exists
            exists = await db.APIUsers.find_one({
                "$or": [
                    {"username": args.username},
                    {"email": args.email}
                ]
            })
            if exists:
                print_error("Username or email already exists")
                return

            # Handle --send-welcome-email flag
            send_welcome = getattr(args, 'send_welcome_email', False)
            if send_welcome and args.role == 'tracker':
                print_warning("--send-welcome-email only applies to admin users. Ignoring flag.")
                send_welcome = False

            # Get password (from argument or interactively)
            if args.password:
                password = args.password
            else:
                password = getpass("Password (min 6 characters): ")

            if len(password) < 6:
                print_error("Password must be at least 6 characters long")
                return

            # Create user
            user_data = {
                "username": args.username,
                "email": args.email.lower(),
                "role": args.role,
                "is_active": True,
                "hashed_password": get_password_hash(password),
                "created_at": datetime.utcnow()
            }

            result = await db.APIUsers.insert_one(user_data)
            print_success(f"User '{args.username}' created successfully!")

            # Send welcome email if requested (only for admin users)
            if send_welcome and args.role == 'admin':
                print_info("Sending welcome email...")

                # Get URLs from environment
                admin_url = os.environ.get('ADMIN_URL', '')
                webapp_url = os.environ.get('WEBAPP_URL', '')

                if not admin_url:
                    print_warning("ADMIN_URL not set. Cannot send welcome email.")
                else:
                    try:
                        # Generate reset token
                        reset_token = secrets.token_urlsafe(32)
                        reset_token_expires = datetime.utcnow() + timedelta(hours=24)

                        # Save token to user document (same format as forgot-password)
                        await db.APIUsers.update_one(
                            {"_id": result.inserted_id},
                            {
                                "$set": {
                                    "reset_token": reset_token,
                                    "reset_token_expires": reset_token_expires
                                }
                            }
                        )

                        # Send welcome email
                        email_service = EmailService()
                        email_sent = await email_service.send_admin_welcome_email(
                            to_email=args.email.lower(),
                            username=args.username,
                            reset_token=reset_token,
                            admin_url=admin_url,
                            webapp_url=webapp_url,
                            contact_email="info@openjornada.es"
                        )

                        if email_sent:
                            print_success(f"Welcome email sent to {args.email}")
                        else:
                            print_warning("Failed to send welcome email. Check SMTP configuration.")
                    except Exception as e:
                        print_warning(f"Error sending welcome email: {str(e)}")
            
        elif args.command == 'delete':
            await delete_user(args.username)
            
        elif args.command == 'role':
            await update_user_role(args.username, args.new_role)
            
        elif args.command == 'password':
            await reset_password(args.username)
            
        elif args.command == 'toggle':
            await toggle_user_status(args.username)
            
        elif args.command == 'show':
            await show_user_details(args.username)
            
        elif args.command == 'list':
            # Same as in the previous script
            users = []
            async for user in db.APIUsers.find():
                users.append(user)

            if not users:
                print_info("No users found")
                return

            print(f"\n{'Username':<20} {'Email':<30} {'Role':<10} {'Active':<8}")
            print("-" * 70)

            for user in users:
                active_status = "Yes" if user.get('is_active', True) else "No"
                print(f"{user['username']:<20} {user['email']:<30} {user['role']:<10} {active_status:<8}")

            print(f"\nTotal users: {len(users)}")

        elif args.command == 'welcome':
            # Find user by email
            user = await db.APIUsers.find_one({"email": args.email.lower()})

            if not user:
                print_error(f"User with email '{args.email}' not found")
                return

            if user.get('role') != 'admin':
                print_error(f"User '{user['username']}' is not an admin (role: {user.get('role')})")
                return

            # Get URLs from environment
            admin_url = os.environ.get('ADMIN_URL', '')
            webapp_url = os.environ.get('WEBAPP_URL', '')

            if not admin_url:
                print_error("ADMIN_URL not set. Cannot send welcome email.")
                return

            print_info(f"Sending welcome email to {user['username']} ({args.email})...")

            try:
                # Generate reset token
                reset_token = secrets.token_urlsafe(32)
                reset_token_expires = datetime.utcnow() + timedelta(hours=24)

                # Save token to user document
                await db.APIUsers.update_one(
                    {"_id": user["_id"]},
                    {
                        "$set": {
                            "reset_token": reset_token,
                            "reset_token_expires": reset_token_expires
                        }
                    }
                )

                # Send welcome email
                email_service = EmailService()
                email_sent = await email_service.send_admin_welcome_email(
                    to_email=args.email.lower(),
                    username=user['username'],
                    reset_token=reset_token,
                    admin_url=admin_url,
                    webapp_url=webapp_url,
                    contact_email="info@openjornada.es"
                )

                if email_sent:
                    print_success(f"Welcome email sent to {args.email}")
                else:
                    print_error("Failed to send welcome email. Check SMTP configuration.")
            except Exception as e:
                print_error(f"Error sending welcome email: {str(e)}")

    except KeyboardInterrupt:
        print_warning("\nOperation cancelled")
    except Exception as e:
        print_error(f"Error: {str(e)}")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
