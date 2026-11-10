# QA Test Planning and Validation Document
## SE Chat Application – Issue #12

**Prepared by:** Rishi Saran Datta – QA Lead  
**Test Engineer:** Abhishek B K Hanapur  
**Issue:** Test Performance, Concurrency and Disconnection Recovery  
**Story Points:** 8  
**Planned Sprint:** Sprint 2 (26–30 October 2026)

---

## 1. Objective

The objective is to verify the performance, concurrent-client support, and recovery capabilities of the SE Chat Application.

Testing will determine whether the application can deliver messages within the required latency, support at least 50 simultaneously authenticated clients, and maintain the availability of other active sessions when an unexpected client disconnection occurs.

## 2. Testing Scope

The following areas are included:

1. Message delivery latency.
2. Concurrent authenticated client handling.
3. Unexpected client disconnection.
4. Session isolation and recovery.
5. Defect identification and regression testing.

## 3. Test Strategy

Testing will use the application’s actual client-server communication and the submitted Software Test Plan.

- Performance testing: Measure message delivery time under normal load.
- Concurrency testing: Verify communication between at least 50 authenticated clients.
- Recovery testing: Forcefully disconnect one client and verify that other clients remain operational.
- Regression testing: Re-execute relevant existing tests after defects are fixed.

## 4. Test Cases

| Test ID | Test scenario | Expected result |
|---|---|---|
| TC-01 | Send messages between two authenticated clients under normal load. | Messages reach the intended recipient. |
| TC-02 | Measure delivery latency for multiple messages. | 90th-percentile latency is at most 1 second. |
| TC-03 | Connect 50 authenticated clients simultaneously. | All 50 clients establish authenticated sessions. |
| TC-04 | Exchange messages between concurrent clients. | Messages are delivered correctly without critical failures. |
| TC-05 | Forcefully disconnect one authenticated client. | Server detects and cleans up the disconnected session. |
| TC-06 | Continue communication between other clients after disconnection. | Other active sessions remain functional. |
| TC-07 | Attempt communication after session disconnection. | Disconnected client cannot continue using its expired session. |
| TC-08 | Run regression tests after defect fixes. | Previously working functionality continues to pass. |

## 5. Requirement Traceability Matrix

| Requirement ID | Requirement | Test Cases |
|---|---|---|
| CHAT-NF-001 | Message delivery latency | TC-01, TC-02 |
| CHAT-NF-002 | Concurrent client support | TC-03, TC-04 |
| CHAT-NF-003 | Client-failure recovery | TC-05, TC-06 |
| CHAT-F-014 | Unexpected client disconnection | TC-05, TC-06, TC-07 |

## 6. Acceptance Criteria

The following conditions determine whether the issue is accepted:

- P90 message delivery latency must be less than or equal to 1000 milliseconds.
- At least 50 authenticated clients must exchange messages without critical failure.
- Disconnecting one client must not interrupt other active sessions.
- Test results and unresolved defects must be documented.
- Fixes must be retested, followed by regression testing.

## 7. Test Result Recording

The following information will be collected during execution:

| Measurement | Actual result |
|---|---|
| Number of authenticated clients | To be recorded |
| Total messages exchanged | To be recorded |
| Average message latency | To be recorded |
| P50 latency | To be recorded |
| P90 latency | To be recorded |
| P99 latency | To be recorded |
| Successful message deliveries | To be recorded |
| Failed message deliveries | To be recorded |
| Disconnection recovery result | To be recorded |
| Regression test result | To be recorded |

## 8. Defect Tracking

Each identified defect will be documented with:

- Defect ID
- Description
- Steps to reproduce
- Expected behaviour
- Actual behaviour
- Severity and priority
- Assigned member
- Fix status
- Retest result

## 9. Retesting and Regression Strategy

After a defect is fixed:

1. Reproduce the original failure.
2. Verify the correction using the same test scenario.
3. Run related test cases to identify side effects.
4. Execute the complete regression test suite.
5. Record the final result.

## 10. Roles and Responsibilities

**QA Lead – Rishi Sar an Datta**

- Define test scope and acceptance criteria.
- Prepare test cases and requirement traceability.
- Coordinate test execution.
- Review test evidence and measurements.
- Maintain defect and retest records.
- Prepare the final QA summary.

**Test Engineer – Abhishek B K Hanapur**

- Develop and execute automated test scenarios.
- Perform latency and concurrency measurements.
- Execute unexpected-disconnection tests.
- Capture test outputs and evidence.
- Report defects and support retesting.

## 11. Final QA Summary

The final QA assessment will be completed after reviewing the actual test execution results.

The application will be considered to satisfy Issue #12 when all acceptance criteria are verified, critical defects are resolved, and the test evidence is documented.

**Status:** Pending test execution and QA review.