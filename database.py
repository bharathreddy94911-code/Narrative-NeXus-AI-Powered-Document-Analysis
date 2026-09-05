import sqlite3
import logging

DATABASE_NAME = 'nexus_users.db'

def get_db_connection():
    """Establishes a connection to the SQLite database."""
    conn = sqlite3.connect(DATABASE_NAME)
    # This allows you to access columns by name, e.g., user['username']
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database and creates the users table if it doesn't exist."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        logging.info("Initializing database...")
        # Create the users table with a unique constraint on the username
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL
            )
        ''')
        conn.commit()
        logging.info("Database initialized successfully. 'users' table is ready.")
    except Exception as e:
        logging.error(f"Error initializing database: {e}")
    finally:
        if conn:
            conn.close()

def add_user(username, password_hash):
    """Adds a new user to the database."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, password_hash))
        conn.commit()
        logging.info(f"User '{username}' added successfully.")
    except sqlite3.IntegrityError:
        logging.warning(f"Attempted to add an existing username: {username}")
        # Re-raise the error so the Flask route can handle it and inform the user
        raise
    finally:
        conn.close()

def get_user(username):
    """Retrieves a user by their username from the database."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
        user = cursor.fetchone()
        return user
    finally:
        conn.close()
