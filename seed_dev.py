"""Seed a customer, user, and a few parts for local testing.
Run once after starting the app locally:  python seed_dev.py
Then visit /login, enter the seeded email; the magic link prints to the console."""
from app import SessionLocal, Customer, CustomerUser, Part

db = SessionLocal()
c = Customer(external_id="GKN-001", name="GKN", active=True)
db.add(c); db.flush()
db.add(CustomerUser(customer_id=c.id, email="buyer@gkn.example", active=True))
for ext, pn, desc in [
    ("P1001", "VSC-44821", "316L manifold block"),
    ("P1002", "VSC-44822", "Ti paddle shifter LH"),
    ("P1003", "VSC-44823", "Ti paddle shifter RH"),
]:
    db.add(Part(customer_id=c.id, external_id=ext, part_number=pn, description=desc, active=True))
db.commit(); db.close()
print("Seeded GKN customer + user buyer@gkn.example + 3 parts.")
