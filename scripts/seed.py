from app.config import Settings
from app.database import database
from app.services.hub import Hub

_, sessions = database(Settings().database_url.get_secret_value())
Hub(sessions).seed_mocks()
print('Seeded two explicitly labelled PixelPapa mock accounts')
