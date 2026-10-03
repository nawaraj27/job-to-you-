"""What a screen reader and a phone get from the CV screens.

Everything here is checked on the HTML the product actually renders, parsed
rather than pattern-matched, so a template that quietly drops a label fails the
test that says it must have one.

What this file cannot do is listen. Nobody has run these pages through a screen
reader, and no parse of the markup is a substitute for that — see the
limitations in `docs/benchmarks/CV-Extraction.md`. What it can do is hold the
wiring that assistive technology depends on: an accessible name on every
control, one `h1` and no skipped levels, a label for every field, alt text on
every image, and the confirmation a swap produces being announced rather than
shown in green and left silent.
"""

from __future__ import annotations

from django.template.loader import render_to_string
from django.urls import reverse
from lxml import html as lxml_html

from apps.resumes.tests.test_analysis import AnalysisFixture

#: Elements that take focus and therefore need a name.
_INTERACTIVE = {"a", "button", "input", "select", "textarea", "summary"}
_FIELDS = {"input", "select", "textarea"}
#: Input types with no user-visible name of their own to give.
_NAMELESS_TYPES = {"hidden", "submit", "button", "reset", "image"}
_HEADINGS = {f"h{level}" for level in range(1, 7)}


def _labels_by_target(tree) -> dict[str, str]:
    return {
        label.get("for"): " ".join(label.itertext()).strip()
        for label in tree.iter("label")
        if label.get("for")
    }


def _accessible_name(element, labels: dict[str, str]) -> str:
    """The name assistive technology would read, in specification order.

    Only the mechanisms this product uses: `aria-label`, `aria-labelledby`, a
    `for=` label, a wrapping label (which is how the radio choices are built),
    `title`, then the element's own text, an input's value, and the alt text or
    SVG title of an icon inside it.
    """
    if (element.get("aria-label") or "").strip():
        return element.get("aria-label").strip()
    if (element.get("aria-labelledby") or "").strip():
        return element.get("aria-labelledby").strip()
    if element.get("id") in labels:
        return labels[element.get("id")]
    for ancestor in element.iterancestors():
        if ancestor.tag == "label":
            return " ".join(ancestor.itertext()).strip()
    if (element.get("title") or "").strip():
        return element.get("title").strip()
    if text := " ".join(element.itertext()).strip():
        return text
    if element.tag == "input" and (element.get("value") or "").strip():
        return element.get("value").strip()
    image = element.find(".//img")
    if image is not None and (image.get("alt") or "").strip():
        return image.get("alt").strip()
    title = element.find(".//{http://www.w3.org/2000/svg}title")
    if title is not None and (title.text or "").strip():
        return title.text.strip()
    return ""


def audit(body: str) -> list[str]:
    """Every accessibility problem this parse can see, as readable lines."""
    tree = lxml_html.fromstring(body)
    labels = _labels_by_target(tree)
    problems: list[str] = []

    counts: dict[str, int] = {}
    for element in tree.iter():
        if isinstance(element.tag, str) and element.get("id"):
            counts[element.get("id")] = counts.get(element.get("id"), 0) + 1
    problems += [
        f"duplicate id {element_id!r} used {count} times"
        for element_id, count in sorted(counts.items())
        if count > 1
    ]

    levels = [
        int(element.tag[1])
        for element in tree.iter()
        if isinstance(element.tag, str) and element.tag in _HEADINGS
    ]
    if not levels:
        problems.append("no headings at all")
    else:
        if levels[0] != 1:
            problems.append(f"the first heading is h{levels[0]}")
        if levels.count(1) != 1:
            problems.append(f"{levels.count(1)} h1 elements")
        problems += [
            f"heading level jumps h{before} to h{after}"
            for before, after in zip(levels, levels[1:], strict=False)
            if after > before + 1
        ]

    for element in tree.iter():
        if not isinstance(element.tag, str):
            continue

        if element.tag in _FIELDS:
            kind = (element.get("type") or "text").lower()
            if element.tag == "input" and kind in _NAMELESS_TYPES:
                continue
            if not _accessible_name(element, labels):
                problems.append(f"unlabelled {element.tag} name={element.get('name')!r}")
        elif element.tag in _INTERACTIVE:
            if element.tag == "a" and element.get("href") is None:
                continue
            if not _accessible_name(element, labels):
                markup = lxml_html.tostring(element, encoding="unicode")[:120]
                problems.append(f"{element.tag} with no accessible name: {markup}")

        if element.tag == "img" and element.get("alt") is None:
            problems.append(f"img with no alt: {element.get('src')}")

        # A handler on something the keyboard cannot reach. Forms are exempt:
        # a form is submitted by its own controls, which are checked above.
        if element.tag in _INTERACTIVE or element.tag == "form":
            continue
        if element.get("tabindex") is not None:
            continue
        trigger = (element.get("hx-trigger") or "").strip()
        if trigger and "click" not in trigger:
            # Fired by an event, a poll or a load — never by a pointer — so
            # there is nothing for the keyboard to reach: a panel that redraws
            # itself when background work finishes is not a button.
            continue
        if element.get("onclick") or element.get("hx-get") or element.get("hx-post"):
            classes = (element.get("class") or "")[:60]
            problems.append(f"{element.tag} is clickable but not focusable: {classes!r}")

    return problems


class EveryCvScreenTests(AnalysisFixture):
    def screens(self) -> dict[str, str]:
        document_id = str(self.document.public_id)
        return {
            "upload": reverse("resume:upload"),
            "your CVs": reverse("resume:builder"),
            "editor": reverse("resume:editor", args=[document_id]),
            "guided builder": reverse("resume:guided", args=[document_id]),
            "assistant": reverse("resume:assistant", args=[document_id]),
            "analysis": reverse("resume:analysis"),
            "templates": reverse("resume:templates"),
            "template compare": reverse("resume:template_compare"),
            "template detail": reverse(
                "resume:template_detail", args=[self.document.template.slug]
            ),
            "exports": reverse("resume:exports"),
            "print preview": reverse("resume:print", args=[document_id]),
        }

    def test_every_cv_screen_is_clean(self):
        self.client.force_login(self.actor)
        for name, url in self.screens().items():
            with self.subTest(screen=name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(audit(response.content.decode()), [])

    def test_the_confirmation_dialog_is_a_labelled_modal(self):
        self.client.force_login(self.actor)
        tree = lxml_html.fromstring(self.client.get(reverse("resume:builder")).content.decode())

        dialog = tree.xpath("//*[@role='alertdialog']")
        self.assertEqual(len(dialog), 1)
        self.assertEqual(dialog[0].get("aria-modal"), "true")
        self.assertTrue(dialog[0].get("aria-labelledby"))
        self.assertTrue(dialog[0].get("aria-describedby"))
        for attribute in ("aria-labelledby", "aria-describedby"):
            with self.subTest(attribute=attribute):
                target = dialog[0].get(attribute)
                self.assertEqual(len(tree.xpath(f"//*[@id='{target}']")), 1)

    def test_there_is_one_live_region_for_the_whole_page(self):
        self.client.force_login(self.actor)
        tree = lxml_html.fromstring(self.client.get(reverse("resume:builder")).content.decode())

        announcer = tree.xpath("//*[@id='announcer']")
        self.assertEqual(len(announcer), 1)
        self.assertEqual(announcer[0].get("role"), "status")
        self.assertEqual(announcer[0].get("aria-live"), "polite")


class ConfirmationsAreAnnouncedTests(AnalysisFixture):
    """A line of green text is silent.

    Every confirmation on a CV screen arrives in an htmx swap — "Made
    primary", "Version saved", "Deleted" — and said nothing to a screen
    reader. `CareerAI.announce` and the live region it writes to were both
    already there, with no caller anywhere in the product.
    """

    def test_a_confirmation_carries_the_text_to_announce(self):
        self.client.force_login(self.actor)
        response = self.client.post(
            reverse("resume:document_act"),
            {"command": "make_primary", "document": str(self.document.public_id)},
        )

        self.assertEqual(response.status_code, 200)
        tree = lxml_html.fromstring(response.content.decode())
        announced = [
            element.get("data-announce")
            for element in tree.xpath("//*[@data-announce]")
            if (element.get("data-announce") or "").strip()
        ]
        self.assertTrue(announced, "the confirmation was rendered with nothing to announce")

    def test_the_message_is_the_one_that_is_announced(self):
        rendered = render_to_string("resume/_flash.html", {"flash_message": "Version saved"})
        tree = lxml_html.fromstring(rendered)

        announced = tree.xpath("//*[@data-announce]")
        self.assertEqual(len(announced), 1)
        self.assertEqual(announced[0].get("data-announce"), "Version saved")

    def test_a_failure_is_an_alert_rather_than_something_to_announce(self):
        # `role="alert"` is read when it appears; announcing it as well would
        # say the same sentence twice.
        rendered = render_to_string("resume/_flash.html", {"flash_error": "That file is too big"})
        tree = lxml_html.fromstring(rendered)

        self.assertEqual(len(tree.xpath("//*[@role='alert']")), 1)
        self.assertEqual(tree.xpath("//*[@data-announce]"), [])


class TheBuilderFitsAPhoneTests(AnalysisFixture):
    """The save-a-version row, which did not.

    Undo, a `w-44` note and "Save a version" came to 368 px inside a 330 px
    card on a 375 px phone, and the 34 px that would not fit pushed the whole
    document sideways — measured in a browser at 375 px on the editor, the
    guided builder and the assistant, all three of which include this partial.

    A layout measurement cannot be made from here, so what is held instead is
    the markup that made it fit: the row wraps, and the note is full-width
    until there is room for it.
    """

    def test_the_save_a_version_row_wraps_and_the_note_shrinks(self):
        self.client.force_login(self.actor)
        document_id = str(self.document.public_id)
        for name, url in (
            ("editor", reverse("resume:editor", args=[document_id])),
            ("guided builder", reverse("resume:guided", args=[document_id])),
            ("assistant", reverse("resume:assistant", args=[document_id])),
        ):
            with self.subTest(screen=name):
                tree = lxml_html.fromstring(self.client.get(url).content.decode())
                note = tree.xpath("//input[@id='version-note']")
                self.assertEqual(len(note), 1)

                classes = (note[0].get("class") or "").split()
                self.assertIn("w-full", classes)
                self.assertIn("min-w-0", classes)
                self.assertNotIn("w-44", classes)

                row = note[0].xpath("ancestor::form[1]/..")[0]
                self.assertIn("flex-wrap", (row.get("class") or "").split())
