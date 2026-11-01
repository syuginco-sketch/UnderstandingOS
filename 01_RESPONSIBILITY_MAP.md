# Responsibility Map

## Purpose

This document defines ownership boundaries within CLOCK.

This document answers:

- Who owns a responsibility?
- Who may consume the result?
- Who must not perform the responsibility?

Detailed behavior is defined in each component document.

This document defines ownership only.

---

# CLOCK Responsibility Model

```text
Reality
  ↓
Recognition
  ↓
Preservation
  ↓
Memory
  ↓
Coordination
  ↓
Authority
  ↓
Decision


Implemented as:

DiverGuard
  ↓
CIRM
  ↓
Clark
  ↓
Spectrum
  ↓
Human Authority


Responsibilities may not overlap.

Information may flow.

Ownership may not.

Responsibility Matrix
Responsibility	Owner	ConsumersRecognition	DiverGuard	CIRM, Spectrum
Preservation	CIRM	Clark, Spectrum
Memory	Clark	Spectrum, Humans
Coordination	Spectrum	Humans
Authority	Humans	Entire Ecosystem
Decision	Humans	Entire Ecosystem
DiverGuard
Owns
Observation
Understanding
Classification
Escalation
Recognition States

Produces:

Observation
Warning
Crime Candidate
Critical
Must Not Own
Preservation
Evidence Storage
Memory
Coordination
Authority

Question:

What is happening?

CIRM
Owns
Preservation
Snapshots
Timeline Capture
Evidence Vault
Evidence Integrity

Produces:

Evidence
Preservation Records
Must Not Own
Recognition
Analysis
Knowledge
Coordination
Authority

Question:

What must not be lost?

Clark
Owns
Collection
Archive
Index
Knowledge Preservation
Historical Retention

Produces:

Archives
Knowledge Assets
Historical Context
Must Not Own
Recognition
Preservation
Monitoring
Authority

Question:

What should survive?

Spectrum
Owns
Coordination
Incident Tracking
Workflow
Visualization
Audit Visibility
AI Assistance

Produces:

Operational Views
Workflow State
Coordination Context
Must Not Own
Recognition
Preservation
Memory Ownership
Authority

Question:

How should people operate?

Human Authority
Owns
Approval
Rejection
Override
Risk Acceptance
Final Decisions

Produces:

Decisions
Must Not Delegate
Accountability

Question:

What shall happen?

Ownership Rules
Rule 1

Recognition ≠ Preservation

Rule 2

Preservation ≠ Memory

Rule 3

Memory ≠ Coordination

Rule 4

Coordination ≠ Authority

Rule 5

Authority remains external.

No CLOCK component owns authority.

Rule 6

If ownership is unclear:

Stop Design
↓
Identify Owner
↓
Continue


Undefined ownership is an architectural defect.

Final Principle
DiverGuard recognizes.
CIRM preserves.
Clark remembers.
Spectrum coordinates.
Humans decide.


No component owns the entire process.

Operational Understanding emerges from separation.
