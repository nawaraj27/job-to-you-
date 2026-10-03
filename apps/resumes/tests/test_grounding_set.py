"""
What the CV assistant may turn a candidate's words into, case by case.

The anti-fabrication rule is deliberately strict and stays so: a bullet may
rephrase what the candidate said, never add to it. This table pins the line
between the two. Each row is what the candidate typed, a bullet a model might
propose, and whether it may be offered.

Allowed: a different register for the same claim ("did" -> "Created"), a
result stated in other words when the candidate gave it ("cut" -> "Reduced",
"went from 30 to 55" -> "Increased … from 30 to 55").

Refused: a number, a tool, a responsibility, a result or an object the
candidate never gave; charge of something they only helped with.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase

from apps.resumes.analysis import target
from apps.resumes.services.assistant import _is_grounded_in

ALLOWED = True
REFUSED = False

CASES = [
    # --- "I did the weekly rota." -----------------------------------------------
    ("I did the weekly rota.", "Did the weekly rota.", ALLOWED),
    ("I did the weekly rota.", "Created the weekly rota.", ALLOWED),
    ("I did the weekly rota.", "Prepared the weekly rota.", ALLOWED),
    ("I did the weekly rota.", "Created the weekly rota for 9 staff.", REFUSED),
    ("I did the weekly rota.", "Created the weekly rota and reduced overtime by 20%.", REFUSED),
    ("I did the weekly rota.", "Managed the weekly staffing rota.", REFUSED),
    ("I did the weekly rota.", "Created the ordering system.", REFUSED),
    # --- "I managed the weekly rota for 9 staff." -------------------------------
    ("I managed the weekly rota for 9 staff.", "Managed the weekly rota for 9 staff.", ALLOWED),
    ("I managed the weekly rota for 9 staff.", "Managed the weekly rota for 12 staff.", REFUSED),
    ("I managed the weekly rota for 9 staff.", "Led a team of 9 staff.", REFUSED),
    # --- "I helped with customer orders." ----------------------------------------
    ("I helped with customer orders.", "Helped with customer orders.", ALLOWED),
    ("I helped with customer orders.", "Managed customer orders.", REFUSED),
    ("I helped with customer orders.", "Led the customer ordering process.", REFUSED),
    ("My manager managed customer orders and I helped.", "Managed customer orders.", REFUSED),
    # --- "Orders increased from 30 to 55." ---------------------------------------
    ("Orders increased from 30 to 55 a day.", "Increased daily orders from 30 to 55.", ALLOWED),
    ("Orders went from 30 to 55 a day.", "Increased orders from 30 to 55 a day.", ALLOWED),
    ("Orders went from 30 to 55 a day.", "Increased orders from 30 to 80 a day.", REFUSED),
    ("Orders went from 30 to 55 a day.", "Increased revenue from 30 to 55 thousand.", REFUSED),
    # --- tools -----------------------------------------------------------------
    (
        "I used Python and Django for the booking site.",
        "Used Python and Django for the booking site.",
        ALLOWED,
    ),
    ("I worked with Python.", "Worked with Python.", ALLOWED),
    ("I worked with Python.", "Built Python and Django applications.", REFUSED),
    ("I helped the team use Docker.", "Helped the team use Docker.", ALLOWED),
    ("I helped the team use Docker.", "Led the team's move to Docker and Kubernetes.", REFUSED),
    ("I helped the team use Docker.", "Managed the team's Docker setup.", REFUSED),
    # --- outcomes ----------------------------------------------------------------
    ("I reduced overtime by 20%.", "Reduced overtime by 20%.", ALLOWED),
    ("I cut overtime by 20% on the weekly rota.", "Reduced overtime by 20%.", ALLOWED),
    ("I reduced overtime by 20%.", "Reduced overtime by 25%.", REFUSED),
    ("I improved the process.", "Improved the process.", ALLOWED),
    # Tolerated: a qualifier with no number, tool, responsibility or result in
    # it. The word-overlap rule has always allowed a minor added word; this row
    # records that rather than pretending it is refused.
    ("I improved the process.", "Improved the ordering process.", ALLOWED),
    ("I improved the process.", "Improved the process, reducing errors.", REFUSED),
    ("I improved the process.", "Improved the process, increasing orders by 40%.", REFUSED),
    # --- "I don't remember the exact number." -----------------------------------
    ("I cut overtime but I don't remember the exact number.", "Reduced overtime.", ALLOWED),
    ("I cut overtime but I don't remember the exact number.", "Reduced overtime hours.", ALLOWED),
    (
        "I cut overtime but I don't remember the exact number.",
        "Reduced overtime by about 20%.",
        REFUSED,
    ),
]


#: Job titles: one the candidate wrote, never one made up from the work.
TITLES = [
    ("I was shift supervisor at Himalayan Java Cafe.", "Shift Supervisor", ALLOWED),
    ("I did the weekly rota.", "Rota Manager", REFUSED),
    ("I managed the weekly rota for 9 staff.", "Rota Manager", REFUSED),
    ("I helped with customer orders.", "Customer Orders Assistant", REFUSED),
]


class GroundingSetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_taxonomy", verbosity=0)
        target.reset_vocabulary()

    @classmethod
    def tearDownClass(cls):
        target.reset_vocabulary()
        super().tearDownClass()

    def test_every_case(self):
        for said, bullet, expected in CASES:
            with self.subTest(said=said, bullet=bullet):
                self.assertEqual(_is_grounded_in(bullet, said), expected)

    def test_every_title(self):
        from apps.resumes.services.assistant import _is_grounded

        for said, title, expected in TITLES:
            with self.subTest(said=said, title=title):
                self.assertEqual(_is_grounded("title", title, said), expected)
