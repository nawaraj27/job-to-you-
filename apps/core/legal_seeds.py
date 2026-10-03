"""
The legal text that ships (SRS §5.14, §15.5).

Here rather than in a template, because a version somebody agreed to has to be
reproducible: `Consent.policy_version` records "0.1-draft", and this is where
0.1-draft can still be read after 0.2 replaces it.

**Every document ships as a draft, and the page says so.** The text below
describes what the platform actually does — it was written against the
implementation, not from a template somebody found — but describing behaviour
accurately is not the same as being legally sufficient in Nepal or under GDPR,
and §1.4 is explicit that GDPR is the design standard rather than a compliance
obligation somebody has signed off. Counsel replaces this text and publishes
1.0; `is_draft=False` is that act, and it is the one thing here nobody should
do without a lawyer.

The privacy text was expanded in P19 to state what the erasure pipeline
actually does, including the two things it cannot undo. A privacy policy that
promised more than §19.4 delivers would be the same defect as a pricing page
advertising a limit nothing enforced.
"""

from __future__ import annotations

#: The version every document ships at. Bumping it is a deliberate act with a
#: changelog line, never a side effect of editing the prose.
DRAFT_VERSION = "0.1-draft"

TERMS_BODY = """
<h2>Your account</h2>
<p>
  You are responsible for the accuracy of what you upload and for keeping your
  login details secure. One account per person; employer accounts must
  represent a real, verifiable organisation.
</p>

<h2>Your content</h2>
<p>
  You keep ownership of everything you upload and create. You grant us only the
  permission needed to store and process it in order to provide the service to
  you.
</p>

<h2>Acceptable use</h2>
<ul>
  <li>Do not upload documents belonging to someone else without their consent.</li>
  <li>Do not post roles that do not exist, or use the platform to collect personal data under false pretences.</li>
  <li>Do not attempt to scrape, overload, or reverse engineer the service.</li>
</ul>

<h2>Plans and payment</h2>
<p>
  Paid plans are invoiced for a period at a time. A cancellation takes effect at
  the end of the period you have paid for, and nothing is withdrawn before then.
  An unpaid invoice does not switch your plan off immediately: we tell you, and
  the plan keeps working for fourteen days while you sort it out.
</p>

<h2>What we don't promise</h2>
<p>
  Scores, matches, and interview feedback are guidance, not guarantees. We
  cannot promise an interview, an offer, or that any employer will respond. No
  hiring decision on this platform is made automatically: a score orders a list,
  and a person decides.
</p>

<h2>Ending the agreement</h2>
<p>
  You can close your account at any time, from your data settings. Closure runs
  after fourteen days and can be cancelled until it starts. We may suspend
  accounts that break these terms, and will tell you why where we are able to.
</p>
""".strip()


PRIVACY_BODY = """
<h2>What we collect</h2>
<ul>
  <li>Account details: your name, email address, and account type.</li>
  <li>Documents you upload, such as CVs, and the text extracted from them.</li>
  <li>Content you create: resumes, applications, cover letters and interview answers.</li>
  <li>Basic technical logs needed to run and secure the service.</li>
</ul>

<h2>What we use it for</h2>
<p>
  Your documents power your own analysis, job matching, and interview practice.
  We do not sell your data, and we do not share your CV with employers unless
  you apply to their role or you opt in to being found in search.
</p>

<h2>AI processing</h2>
<p>
  Some features send parts of your documents to an AI provider. Contact details
  are removed before anything is sent. Every such call is recorded against your
  account so you can see what was run, and the text we hold from those calls is
  deleted after thirty days.
</p>

<h2>How long we keep things</h2>
<ul>
  <li>Your documents and profile: for the life of your account.</li>
  <li>Interview transcripts: twelve months.</li>
  <li>Applications and the notes an employer made on them: twenty-four months after the application closes.</li>
  <li>Rendered CV files: ninety days, then regenerated on demand.</li>
  <li>A CV uploaded before you create an account: twenty-four hours, and it is never processed.</li>
</ul>

<h2>Your control</h2>
<ul>
  <li>Export your profile, resumes and application history at any time.</li>
  <li>Erase your profile, keeping the account.</li>
  <li>Close the account entirely. It runs after fourteen days, and until then you can stop it.</li>
</ul>

<h2>What closing your account removes</h2>
<p>
  Your CVs and the text taken from them, your analyses, your interview answers,
  the contents of your messages, your profile, and the text of anything we sent
  to an AI provider on your behalf. Applications stay with the employers who
  received them as a record that somebody applied, with none of your personal
  details left in them.
</p>

<h2>Two things we cannot undo</h2>
<p>
  A CV already delivered to an employer stays with that employer — we cannot
  reach into their records. And text already sent to an AI provider cannot be
  unsent: our copy is deleted, theirs is subject to their own retention.
</p>

<h2>Contact</h2>
<p>
  Questions about your data: <a href="mailto:hello@careerai.test">hello@careerai.test</a>.
</p>
""".strip()


#: (slug, version, title, summary, body)
DOCUMENTS: list[tuple[str, str, str, str, str]] = [
    (
        "terms",
        DRAFT_VERSION,
        "Terms of use",
        "First draft, written against the implementation. Not yet reviewed by counsel.",
        TERMS_BODY,
    ),
    (
        "privacy",
        DRAFT_VERSION,
        "Privacy policy",
        "First draft. States the retention schedule and the erasure pipeline as built.",
        PRIVACY_BODY,
    ),
]
