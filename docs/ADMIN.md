# Django admin inventory

All 124 project models are covered: 119 have their own admin page, 3 are
edited as inlines of their parent, and 2 are kept out of the admin on purpose
(decision D-01: message text and company verification documents). `apps/core/tests/test_admin_inventory.py`
fails when a new model is added without a decision about how staff see it.

## Rules

- **The admin is for staff with model permissions.** `is_staff` alone opens no
  model page; each page needs that model's `view` permission or a superuser
  (decision D-01). A `view` permission grants viewing only.
- **Records are view-only.** This covers outbox events, deliveries, consents,
  messages, payments, audit rows and so on. Add, change and delete are refused
  on the server (`apps.common.admin_tools.RecordAdmin`). The writes staff
  really need are admin actions or staff-portal actions that go through the
  services, so they are audited and emit their events. Examples: record a bank
  transfer, void an invoice, re-check a gateway payment, end a subscription.
- **Private content is never rendered.** "Hidden fields" below are left out of
  list pages, detail pages, search and page titles. For models "named by id",
  `__str__` (which quotes content) is replaced by type and id. Reading a
  candidate's words is a staff-portal action behind a `StaffRole` and an access
  log.
- **Credentials:**
  - `OTP_ADMIN_HIDE_SENSITIVE_DATA = True` and view-only MFA devices: no TOTP
    key, QR code or backup tokens.
  - Passwords show only Django's masked hash summary.
  - Webhook secrets, session keys, invite token hashes, idempotency keys and
    gateway credentials are never shown. Gateway credentials live in the
    environment and appear in the payment-attempt admin only as
    configured/not configured.
- **Editable** is kept for reference and configuration data: plans, coupons,
  feature flags, CV templates, interview templates, the skills taxonomy and AI
  quotas.

## Inventory

| Model | Admin | Hidden fields |
|---|---|---|
| accounts.Consent | View only | - |
| accounts.EmailAddress | View only | - |
| accounts.IdempotencyKey | View only | key, request_hash, response_ref |
| accounts.StaffRole | View only | - |
| accounts.StaffRoleRequest | View only | - |
| accounts.User | Editable | - |
| accounts.UserPreferences | View only | - |
| accounts.UserSession | View only (named by id) | session_key |
| ai.AIFeedback | View only | - |
| ai.AIRun | View only | - |
| ai.AIUsageQuota | Editable | - |
| ai.PromptTemplate | View only | - |
| analytics.AICostDaily | View only | - |
| analytics.AnalyticsEvent | View only | - |
| analytics.CompanyDailyStat | View only | - |
| analytics.FunnelSnapshot | View only | - |
| analytics.JobDailyStat | View only | - |
| analytics.SteeringSnapshot | View only | - |
| applications.Application | View only | - |
| applications.ApplicationEvent | View only | - |
| applications.ApplicationNote | View only (named by id) | body, mentions |
| applications.ApplicationRating | View only | comment |
| applications.ApplicationTag | View only | - |
| applications.CandidateVisibilityGrant | View only | - |
| applications.CompanyCandidateNote | View only (named by id) | body |
| applications.CoverLetter | View only (named by id) | content, title |
| applications.Offer | View only | - |
| applications.PipelineStage | View only | - |
| applications.Tag | View only | - |
| applications.TalentPool | View only | - |
| applications.TalentPoolMember | View only | - |
| audit.AuditLog | View only | - |
| billing.Coupon | Editable | - |
| billing.Invoice | View only | - |
| billing.Payment | View only | - |
| billing.PaymentAttempt | View only | - |
| billing.Plan | Editable | - |
| billing.Subscription | View only | - |
| billing.UsageCounter | View only | - |
| candidates.BlockedCompany | View only | reason |
| candidates.CandidateProfile | View only | phone, summary, headline, salary_min, salary_max, salary_min_base, salary_max_base, fx_rate_id, fx_rate_value, fx_as_of, fx_source, open_to, embedding, embedding_model, embedded_at, search_vector, location |
| candidates.CandidateSkill | View only | evidence_ref |
| candidates.ProfileAward | View only (named by id) | name, issuer, summary, awarded_on |
| candidates.ProfileCertification | View only (named by id) | name, issuer, credential_id, credential_url, file, issued_on, expires_on |
| candidates.ProfileEducation | View only (named by id) | institution, qualification, field_of_study, summary, start_date, end_date |
| candidates.ProfileExperience | View only (named by id) | title, org_name, summary, bullets, location, company, start_date, end_date |
| candidates.ProfileLanguage | View only | - |
| candidates.ProfileLink | View only (named by id) | url, label |
| candidates.ProfileProject | View only (named by id) | name, url, summary, started_on, finished_on |
| candidates.ProfileView | View only | - |
| candidates.SavedJob | View only | note |
| candidates.SavedSearch | View only | query, name |
| common.ConsumedToken | View only | - |
| companies.Company | View only | - |
| companies.CompanyDomain | View only | - |
| companies.CompanyFollow | View only | - |
| companies.CompanyInvite | View only | token_hash |
| companies.CompanyMembership | View only | - |
| companies.CompanyUpdate | View only | - |
| companies.CompanyVerification | View only | - |
| companies.CompanyVerificationDecision | View only | documents |
| companies.CompanyVerificationDocument | **Not in the admin** (decision D-01: staff portal's audited verification review only) | - |
| companies.OwnershipTransfer | View only | - |
| core.FeatureFlag | Editable | - |
| core.LegalDocument | View only | - |
| files.Attachment | View only | - |
| files.AttachmentAccess | View only | - |
| files.AttachmentGrant | View only | - |
| interviews.InterviewEvaluation | View only | - |
| interviews.InterviewOutcome | View only | - |
| interviews.InterviewParticipant | View only | - |
| interviews.InterviewRound | Editable | - |
| interviews.InterviewSession | View only | - |
| interviews.InterviewSlot | View only | - |
| interviews.InterviewTemplate | Editable | - |
| interviews.InterviewTurn | View only | - |
| interviews.InterviewerAvailability | Editable | - |
| jobs.Job | View only | - |
| jobs.JobAlertMatch | View only | - |
| jobs.JobAssignment | Inline on jobs.Job | - |
| jobs.JobScreeningQuestion | View only | - |
| jobs.JobSkill | Inline on jobs.Job | - |
| jobs.JobView | View only | - |
| messaging.Conversation | View only (named by id) | subject |
| messaging.ConversationReport | View only | - |
| messaging.Message | **Not in the admin** (decision D-01: message text only through the staff portal, for reported conversations) | - |
| notifications.EmailMessage | View only | params |
| notifications.EmailSuppression | View only | - |
| notifications.FanoutJob | View only | - |
| notifications.Notification | View only (named by id) | title, body, payload, target_url, dedupe_key |
| notifications.NotificationDelivery | View only | - |
| notifications.NotificationPreference | View only | - |
| notifications.OutboxConsumerRun | View only | - |
| notifications.OutboxEvent | View only | payload |
| notifications.WebhookDelivery | View only | response_excerpt |
| notifications.WebhookEndpoint | View only | secret |
| resumes.BuilderSession | View only | transcript, step_state |
| resumes.ImprovementJob | View only | payload, result, request_token, stage_detail |
| resumes.ResumeAnalysis | View only | - |
| resumes.ResumeAtsCheck | View only | note |
| resumes.ResumeDocument | View only | - |
| resumes.ResumeDraft | View only | - |
| resumes.ResumeDraftSnapshot | View only (named by id) | content |
| resumes.ResumeFinding | View only | - |
| resumes.ResumeImportProposal | View only | - |
| resumes.ResumeParse | View only | - |
| resumes.ResumeRender | View only | - |
| resumes.ResumeSuggestion | View only (named by id) | original, proposed, rationale |
| resumes.ResumeTemplate | Editable | - |
| resumes.ResumeUpload | View only | - |
| resumes.ResumeVersion | View only | - |
| taxonomy.FxRate | View only | - |
| taxonomy.ImportRun | View only | - |
| taxonomy.Industry | Editable | - |
| taxonomy.JobCategory | Editable | - |
| taxonomy.Language | Editable | - |
| taxonomy.Location | Editable | - |
| taxonomy.Skill | Editable | - |
| taxonomy.SkillAlias | Editable | - |
| taxonomy.SkillCategory | Editable | - |
| taxonomy.SkillExternalId | Editable | - |
| taxonomy.SkillRelation | Editable | - |
| taxonomy.SkillTranslation | Inline on taxonomy.Skill | - |
| taxonomy.UnresolvedTerm | Editable | - |
