from database import get_connection
from werkzeug.security import generate_password_hash


TEACHERS = {
    "Swapna": "swapna",
    "Venkat Raja": "venkatraja",
    "Subrahmanya": "subrahmanya",
    "Raja": "raja",
    "Yashodha": "yashodha",
    "Gomathi": "gomathi",
    "Aradhya": "aradhya",
    "Kalavathi": "kalavathi",
    "Dyamanna": "dyamanna"
}

TEMP_PASSWORD = "Teacher@123"


def create_teacher_logins():

    connection = get_connection()
    cursor = connection.cursor()

    for teacher_name, username in TEACHERS.items():

        password_hash = generate_password_hash(TEMP_PASSWORD)

        cursor.execute(
            """
            UPDATE teachers
            SET username = %s,
                password_hash = %s,
                role = 'TEACHER'
            WHERE teacher_name = %s
            """,
            (username, password_hash, teacher_name)
        )

        if cursor.rowcount == 1:
            print(f"Login created: {teacher_name} -> {username}")
        else:
            print(f"Teacher not found: {teacher_name}")

    connection.commit()

    cursor.close()
    connection.close()

    print()
    print("====================================")
    print("Teacher logins created successfully!")
    print("Temporary password: Teacher@123")
    print("====================================")


if __name__ == "__main__":
    create_teacher_logins()