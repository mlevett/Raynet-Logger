from app import create_backup, init_db
init_db()
path = create_backup()
print(path)
