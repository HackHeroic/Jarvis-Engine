-- The typed reason behind a suggestion's consent decision (capstone plan 2, decision 12).
--
-- consent_reason holds the sentence the user reads; consent_kind says which link of
-- the consent chain decided it (entity ConsentReasonKind), so the app can show a
-- reason by type. Suggestions stored before this column read as NULL, which the
-- entity reads as "no kind recorded"; a CHECK passes NULL, so only the four kinds or
-- nothing can be stored.
--
-- Additive and idempotent: a re-run adds nothing and re-creates the same CHECK.

alter table public.proactive_suggestions
    add column if not exists consent_kind text;

alter table public.proactive_suggestions
    drop constraint if exists proactive_suggestions_consent_kind_check;
alter table public.proactive_suggestions
    add constraint proactive_suggestions_consent_kind_check
    check (consent_kind in ('rule_match', 'mode_decision', 'risk_check', 'learned_preference'));

comment on column public.proactive_suggestions.consent_kind is
    'Which consent rule decided: rule_match, mode_decision, risk_check or learned_preference; '
    'NULL on rows stored before it was recorded.';
