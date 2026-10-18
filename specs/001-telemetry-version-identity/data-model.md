# Data Model — F1

- ProducerVersion: nonempty installed_version, optional source_revision; immutable for a process.
- IdentitySnapshot: resolved_state_root (local only), identity_was_present, install_id if valid,
  integrity digest (local only). If previously present, missing/corrupt/mismatch is an error.
- Envelope: bubo_version, install_id, distinct_id, existing allowlisted properties; source version immutable.
  Existing PostHog identity fields retain their exact values. Event IDs are delivery identifiers only.

Identity transitions: absent -> initialized only through normal first initialization; present -> preserved
through upgrade; present -> invalid blocks update/recovery until original identity is recovered.
No database table migration is needed for this feature.
