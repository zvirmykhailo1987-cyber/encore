# Security and privacy

**This is an engineer's reading, not legal advice. No lawyer or DPO has reviewed it.** Encore has only ever held synthetic data, so nothing here has been tested on real personal data. Conclusions marked "my reading" have no source that says exactly this.

## Who is responsible for what

| Question | My reading | Needs confirming |
|---|---|---|
| Who controls attendee contacts? | The organizer is controller, and the platform is processor | Whether the platform also runs a shared buyer account. Then it may be a controller or joint controller |
| Merging one person across organizers? | **Not done.** A processor deciding its own purpose [can become a controller](https://www.edpb.europa.eu/system/files/documents/2023-10/EDPB_guidelines_202007_controllerprocessor_final_en.pdf), and [soft opt-in does not travel between businesses](https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32002L0058) | DPO |
| External ops team? | May be a sub-processor if it can reach personal data | Contract and customer notice |
| Is a DPIA needed? | The duty is the controller's, and the platform helps. The migration record below is the input | DPO |

## Personal data in this design

| Data | Where | Why | Protection |
|---|---|---|---|
| Name, email, phone, city | `crm_contact` | The CRM's purpose | Only through imports, by that organizer's key or a platform key |
| Consent yes or no, when, wording | `crm_contact` | [Proof must show when and how](https://www.edpb.europa.eu/system/files/documents/files/file1/edpb_guidelines_202005_consent_en.pdf) | As above |
| Broken rows, in raw form | `imports_quarantinerow` | A person must fix them | Organizer or platform key. Every read is logged. Cleared on erasure |
| Earlier values | `imports_contactchange` | Rollback | Removed with the contact. **Needs deletion once rollback is over: not built** |
| Holder email | `ticketing_ticket` | Comes from the ticket feed | Not in the gate manifest |
| Hash of an erased email | `crm_erasedsubject` | Keeps them erased | Pseudonymous, so still personal data. Needs a retention decision |
| Export files | The import folder | Source of the migration | **Delete after cutover: manual today** |
| Gate device | Ticket id and status only | Offline admission | No names or emails on the device |

Logs carry the request path and id, not bodies. Slow request logs include the path, so identifiers must stay out of URLs.

## Rights and duties during a migration

| Situation | What happens | Status |
|---|---|---|
| Consent proof | When and wording travel with the contact. Unknown stays NULL, never yes or no. Gaps are counted before cutover | **Built** |
| Erasure after the export, before cutover | The contact is deleted, the ledger blocks re-import, quarantine is cleared | **Built** |
| Erasure after a backup was taken | `replay_erasures` applies the ledger again. [EDPB expects this on restored systems](https://www.edpb.europa.eu/system/files/2026-02/edpb_cef-report_2025_right-to-erasure_en.pdf) | **Built**, tested by simulation, not on a real backup |
| Rollback | Snapshots of erased people are deleted with them | **Built** |
| Erasure by phone only | Not supported: email only | Gap |
| Search index, other copies | Must get the erasure too | Design |
| Migration loses rows, or shows one organizer's data to another | [Permanent loss is an availability breach](https://www.edpb.europa.eu/system/files/documents/2023-04/edpb_guidelines_202209_personal_data_breach_notification_v2.0_en.pdf), so this may be a personal data breach, not only a bug. See below | Runbook |
| Old CRM at the end | [Delete and confirm deletion in an agreed way](https://www.edpb.europa.eu/system/files/documents/2023-10/EDPB_guidelines_202007_controllerprocessor_final_en.pdf) | Design |
| Data leaving the EEA | Unknown: depends on the real hosting, ops team and tools | Question |
| Minors | No date of birth in this model. [Age rules vary by country](https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32016R0679) where consent is the basis, so the real data may need age-based marketing suppression | Design |

## Breach path

A processor must tell the controller without undue delay, and the [controller has 72 hours](https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32016R0679) from becoming aware.

1. **Stop.** Pause all imports and cutovers. Do not delete evidence.
2. **Tell the DPO**, the same hour.
3. **Establish the facts.** Which organizers, which people, how many, since when. The run id gives the exact file and rows.
4. **Contain.** Roll back the run if it is the latest. Restrict access.
5. **Notify** the affected organizers, as the DPO decides.
6. **Write it down**, even if it turns out not to be notifiable.

## Card data

Encore stores no payment data, and neither should a CRM. Legacy notes fields can contain pasted card numbers, so **any column** is checked with a digit pattern and the Luhn test. A match is quarantined with no content kept, so the number reaches neither the new CRM nor a log. This is a catch for accidents, and it can miss numbers written in unusual ways or flag long digit strings by chance. If real card numbers are found, the source system has a [PCI DSS](https://www.pcisecuritystandards.org/faqs/1280/) problem of its own. I could not read the PCI DSS 4.0.1 text directly, and whether closed-loop wristbands are in scope is a question for the acquirer.

## Threats considered

| Threat | Control | Test or evidence |
|---|---|---|
| Forged or replayed webhook | HMAC on raw bytes, 5 minute window | `test_webhook_signature_rules`, bug injection |
| Calls without authority | Closed by default: every API route needs a key unless it is named public. Keys are 256 random bits, stored as a digest, revocable | `test_every_api_route_refuses_a_caller_without_a_key`, `test_unknown_and_revoked_keys_are_refused` |
| Guessing keys, or flooding | Per-key limit, and a per-address limit on calls without a valid key | `test_callers_without_a_key_are_slowed_down`, `test_each_key_has_its_own_limit` |
| A key doing more than its job | Three scopes: platform, organizer, gate | `test_a_gate_key_scans_but_reads_no_imports_and_runs_no_operations`, `test_an_organizer_key_cannot_scan_or_run_the_connector` |
| No record of who read personal data | Access log with key prefix, path and status; purged after a set period | `test_personal_data_reads_and_changes_are_logged_with_the_key` |
| Another site calling a debug machine | Origin check | `test_debug_machine_refuses_writes_from_another_website` |
| Reading any file through an import | Sources resolved inside one folder only | `test_import_api_cannot_read_outside_the_source_folder` |
| One organizer reading another's data | Organizer keys see their own runs, rows and source folder only. Anything else answers 404 | `test_another_organizers_run_answers_404_everywhere`, `test_an_organizer_key_imports_for_itself_only` |
| The documented API drifting from the code | A test compares every route's scope in `docs/openapi.yaml` with the code | `test_openapi_documents_every_route_with_the_scope_the_code_enforces` |
| Cross-site scripting from imported data | The console inserts data only as text, and a strict CSP blocks inline scripts outside debug | `tests/test_console.py` |
| Secrets in code | No usable defaults. The app refuses to start without them. Secret scanner on commit | `encore/settings.py` |
| SQL injection | The ORM, or parameters. Table names in reconciliation come from an integer primary key | Lint rule S608 reviewed |
| Dependency vulnerabilities | Pinned. `pip-audit` result in the evidence section | |
| Container | Runs as non-root, except nginx | `deploy/k8s` |
| Metrics exposure | Edge refuses `/metrics`, network policy limits access | `docs/k8s-proof.json` |

## Not built

Personal user accounts and single sign-on: a key stands for a team or a device. Encryption of the export folder. Retention of contact data. A penetration test. **Real personal data does not belong in this version.**
