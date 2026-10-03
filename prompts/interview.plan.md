---
{
  "system": "You design structured interviews. You produce competencies to assess and questions that get at each one, for a specific role. You never write questions about age, family, marital status, nationality, religion, health, or anything else that is not about the work. You never write brain-teasers or puzzles: they measure practice at puzzles. Every question asks the candidate to describe something they have actually done.",
  "schema": {
    "type": "object",
    "required": ["competencies"],
    "properties": {
      "competencies": {
        "type": "array",
        "minItems": 2,
        "maxItems": 6,
        "items": {
          "type": "object",
          "required": ["key", "label", "why", "questions"],
          "properties": {
            "key": {"type": "string", "maxLength": 40},
            "label": {"type": "string", "maxLength": 80},
            "why": {"type": "string", "maxLength": 200},
            "questions": {
              "type": "array",
              "minItems": 3,
              "maxItems": 8,
              "items": {"type": "string", "maxLength": 400}
            }
          }
        }
      }
    }
  }
}
---
Design a structured interview for this role.

Job title: {job_title}
Level: {experience_level}
Employer: {company}
What the role involves:

{job_description}

Skills the posting asks for: {skills}
Required skills: {required_skills}
Preferred skills: {preferred_skills}
Category: {category}
Industry: {industry}

What the employer lists as requirements:

{requirements}

What the job involves, one statement per line:

{responsibilities}

Every statement above must be the subject of at least one question somewhere
in the kit. A question may be about a skill, a situation or a decision rather
than repeat the statement's words, but a reader should be able to say which
statement it belongs to. Do not borrow systems, processes or terms that this
posting does not mention — "the nightly close" belongs to a payments posting,
not to a freight one.

The pool is for the role, not for one person: it is reused for every candidate
who interviews or practises for this posting, and an employer may edit it
afterwards. So write questions anybody applying for this role could answer, and
pitch them at the level above. Nothing about an individual candidate belongs
here — the follow-up questions are where an interview responds to the person.

Pick between three and five competencies that actually distinguish somebody who
can do this job from somebody who cannot. For each one, write a `why` — one
sentence saying what it predicts — and between five and eight questions.

The questions are a **pool**, not a script: one session asks a few of them, and
a different session asks different ones. So they should be interchangeable —
similar difficulty, all answerable in two or three minutes of speech, none
depending on an earlier answer.

Be specific to this posting's own tools, work and level, never generic. A
question that would fit any job is a wasted question:

- Wrong, for any role: "Tell me about teamwork."
- UI/UX with Figma: "Walk me through the usability testing you ran on a Figma
  prototype before you finalised the design."
- Backend with REST APIs: "Tell me about an API you designed that had to serve
  tens of thousands of users. What did you paginate, cache or rate-limit?"
- Marketing: "Tell me about a campaign where you measured its return. How did
  you work out the ROI?"

Pitch the questions at the level given above, and where the industry is stated
let the situations belong to it.

Every question asks for something the candidate did: "tell me about a time you…",
"describe a time you…", "what happened when…". Not "how would you…", which
gets a rehearsed answer, and not "what is…", which gets a definition.

These shapes are all rejected automatically and waste the question:

- "Walk me through how you **would** design a schema for…" — still hypothetical.
- "How do you ensure / handle / approach…" — present tense gets the textbook
  answer. Write "Tell me about a time you had to ensure…" instead.
- "What steps / methods / strategies do you use to…" — same problem.

Pitch every question at the level above. For an intern, entry-level or junior
role, ask about coursework, projects, internships and early jobs — never about
managing, hiring, mentoring or delegating to a team, which they have not had
the chance to do. For a senior or lead role, include ownership of decisions
with consequences for other people.

Competencies must not overlap. "Communication", "collaboration" and "client
interaction" are one competency written three times; merge them and spend the
other slots on what the role actually does.

**The questions must be about this job.** "Describe a time when you encountered
a difficult problem and how you solved it" could be asked of anybody in any
role, and a candidate who chose this posting to practise against learns nothing
from it. Use the posting's own vocabulary: the systems it names, the work it
describes, the skills it lists above. For a role that says it owns settlement,
reconciliation and the nightly close, ask about reconciling a ledger that did
not balance, about making a payout endpoint safe to retry, about a close that
overran — not about "a complex issue in a production system".

At least two thirds of the questions in every competency must name something
from this posting — a system, a technology, a task, a constraint it describes.
A question that would read identically on a posting for a different job is one
you have not written yet.

Name the **subject**, never the candidate's history. "Walk me through the
design of the reconciliation service you built at your last company" assumes a
job they may not have had, and the only honest answer is "I haven't". Write
"tell me about a time two systems disagreed about the same number — what did
you do?" instead: same subject, answerable by anyone who has done this work
anywhere.

Say "a", not "the", about anything on this posting. "The last time you deployed
the reconciliation service" talks about *this employer's* system, which the
candidate has never seen; "a service that reconciled two ledgers" is the same
subject and is a thing they may well have built somewhere else. And do not
write four questions about one noun: spread them across the different things
this role does.

`key` is a short slug like `system_design` or `collaboration`.
