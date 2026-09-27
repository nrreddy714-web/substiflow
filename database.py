import mysql.connector
from dotenv import load_dotenv
import os

load_dotenv()


def normalize_db_name(value):
    if not value:
        return "smart_timetable"
    return value.strip().strip("`").replace("`", "")


def get_connection():
    config = {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": normalize_db_name(os.getenv("DB_NAME", "smart_timetable")),
    }

    # Aiven requires an encrypted MySQL connection.
    # Set DB_SSL=1 on Render; local development remains unchanged by default.
    if os.getenv("DB_SSL", "0").strip().lower() in ("1", "true", "yes", "on"):
        config["ssl_disabled"] = False
        ssl_ca = os.getenv("DB_SSL_CA", "").strip()
        if ssl_ca:
            config["ssl_ca"] = ssl_ca
            config["ssl_verify_cert"] = True
            config["ssl_verify_identity"] = True
        else:
            # TLS is still used; certificate verification can be enabled by
            # supplying the Aiven CA certificate through DB_SSL_CA.
            config["ssl_verify_cert"] = False
            config["ssl_verify_identity"] = False

    return mysql.connector.connect(**config)


def test_connection():
    try:
        connection = get_connection()

        if connection.is_connected():
            print("Database connected successfully!")

        connection.close()

    except mysql.connector.Error as error:
        print("Database connection failed:", error)


if __name__ == "__main__":
    test_connection()