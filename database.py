import mysql.connector
from dotenv import load_dotenv
import os

load_dotenv()


def get_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "smart_timetable")
    )


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