// jobs.qnt — laws for the durable, claimable-job framework (issue #16).
//
// A job is a row with a lifecycle status. The runner claims a PENDING (or due
// SCHEDULED) job with one **conditional write** (status == PENDING), runs the
// body, and finishes with another conditional write (status == RUNNING AND
// claimed_by == this runner). This spec pins the small, pure rules the
// coordination rests on:
//
//   - claimSingleWinner: of any number of runners racing to claim one PENDING
//     job, at most one wins -- the conditional write is a single atomic
//     operation, so a losing runner sees Absent (indistinguishable from an
//     absent row) and claims nothing.
//   - claimNeedsRunnable: only a PENDING job (or a due SCHEDULED one, promoted
//     first) is claimable; a RUNNING / terminal job never is.
//   - terminalSticky: a terminal status (COMPLETED / ERROR / CANCELLED) never
//     transitions -- no client update and no runner completion can move it.
//   - completionOnlyIfOwner: a completion applies only while the runner still
//     owns the claim (status == RUNNING and claimed_by == the runner); a
//     recovered / re-claimed job refuses a late completion, so no terminal
//     state is ever overwritten.
//   - attemptCap: a failed run requeues to PENDING only while attempts <
//     max_attempts; at the cap it goes terminal ERROR. The attempt counter
//     increments once per claim.
//   - staleRecoverable: a RUNNING claim older than its bound is recovered to
//     PENDING (under the cap) or terminal ERROR (at the cap), so no job is ever
//     stuck RUNNING.
//   - scheduleGate: a SCHEDULED job is promoted to PENDING only once its run_at
//     is due.
//   - runnerGate: with the runner disabled, a sweep claims nothing.
//
// The status / attempt / claim machinery is unrolled over a finite universe,
// matching how background_tasks.qnt / triggers.qnt unroll theirs.

module jobs {
  // The lifecycle status. PENDING / SCHEDULED are claimable; RUNNING is claimed;
  // the rest are terminal.
  type Status = Pending | Scheduled | Running | Completed | Error | Cancelled

  pure def isTerminal(s: Status): bool =
    match s {
      Completed => true | Error => true | Cancelled => true |
      Pending => false | Scheduled => false | Running => false
    }

  pure def isRunnable(s: Status): bool = s == Pending

  // ---- Single-winner claim -------------------------------------------------
  // The claim is one conditional write: it applies only to a still-PENDING row.
  // Model a race by the number of runners attempting it; the winner count is 1
  // when the job was PENDING and 0 otherwise, never more than 1.
  pure def claimWinners(runners: int, status: Status): int =
    if (isRunnable(status) and runners > 0) 1 else 0

  pure def atMostOneWinner(runners: int, status: Status): bool =
    claimWinners(runners, status) <= 1

  // ---- Terminal stickiness -------------------------------------------------
  // A status transition requested by a client update / runner completion is
  // refused when the row is already terminal -- the effective status is
  // unchanged.
  pure def transition(current: Status, requested: Status): Status =
    if (isTerminal(current)) current else requested

  // ---- Completion ownership ------------------------------------------------
  // A completion applies only when the row is RUNNING and still owned by this
  // runner; otherwise it is refused (the effective status is unchanged).
  pure def complete(current: Status, ownedByRunner: bool, requested: Status): Status =
    if (current == Running and ownedByRunner and not(isTerminal(current)))
      requested
    else
      current

  // ---- Attempt cap ---------------------------------------------------------
  // After a run, a failure requeues while attempts < max_attempts, else errors.
  pure def afterFailure(attempts: int, maxAttempts: int): Status =
    if (attempts < maxAttempts) Pending else Error

  // A success is terminal COMPLETED regardless of attempts.
  pure def afterSuccess(_attempts: int, _maxAttempts: int): Status = Completed

  // ---- Stale recovery ------------------------------------------------------
  // A RUNNING claim whose age exceeds its bound is recovered: requeued under the
  // cap, terminal ERROR at the cap.
  pure def recover(attempts: int, maxAttempts: int): Status =
    if (attempts < maxAttempts) Pending else Error

  pure def isStale(ageSeconds: int, boundSeconds: int): bool = ageSeconds > boundSeconds

  // ---- Schedule gate -------------------------------------------------------
  pure def promotes(runAtDue: bool): bool = runAtDue

  // ---- Runner gate ---------------------------------------------------------
  pure def sweeps(runnerEnabled: bool): bool = runnerEnabled

  // ---- Tests ---------------------------------------------------------------

  // Of any number of racing runners, at most one claims a PENDING job; a
  // non-PENDING job is claimed by nobody.
  run claimSingleWinner = {
    all {
      assert(claimWinners(1, Pending) == 1),
      assert(claimWinners(3, Pending) == 1),
      assert(claimWinners(3, Running) == 0),
      assert(claimWinners(3, Completed) == 0),
      assert(claimWinners(3, Cancelled) == 0),
      assert(claimWinners(0, Pending) == 0),
      assert(atMostOneWinner(5, Pending)),
      assert(atMostOneWinner(5, Running)),
    }
  }

  // Only a PENDING job is runnable; every other status is not.
  run claimNeedsRunnable = {
    all {
      assert(isRunnable(Pending)),
      assert(not(isRunnable(Scheduled))),
      assert(not(isRunnable(Running))),
      assert(not(isRunnable(Completed))),
      assert(not(isRunnable(Error))),
      assert(not(isRunnable(Cancelled))),
    }
  }

  // A terminal status never transitions, whatever is requested.
  run terminalSticky = {
    all {
      assert(isTerminal(Completed) and isTerminal(Error) and isTerminal(Cancelled)),
      assert(transition(Completed, Pending) == Completed),
      assert(transition(Error, Pending) == Error),
      assert(transition(Cancelled, Running) == Cancelled),
      // A non-terminal status does transition.
      assert(transition(Pending, Cancelled) == Cancelled),
      assert(transition(Running, Completed) == Completed),
    }
  }

  // A completion applies only while the runner still owns a RUNNING claim.
  run completionOnlyIfOwner = {
    all {
      assert(complete(Running, true, Completed) == Completed),
      assert(complete(Running, true, Error) == Error),
      // A recovered / re-claimed job refuses the late completion.
      assert(complete(Running, false, Completed) == Running),
      // A job another runner already finished stays terminal.
      assert(complete(Completed, false, Error) == Completed),
      assert(complete(Pending, true, Completed) == Pending),
    }
  }

  // A failed run requeues under the cap and errors at it.
  run attemptCap = {
    all {
      assert(afterFailure(1, 2) == Pending),
      assert(afterFailure(2, 2) == Error),
      assert(afterFailure(3, 2) == Error),
      assert(afterFailure(1, 1) == Error),
      assert(afterSuccess(1, 1) == Completed),
    }
  }

  // A stale claim is recovered to a non-RUNNING status: PENDING under the cap,
  // ERROR at it. Recovery never leaves it RUNNING.
  run staleRecoverable = {
    all {
      assert(isStale(7200, 3600)),
      assert(not(isStale(10, 3600))),
      assert(recover(1, 2) == Pending),
      assert(recover(2, 2) == Error),
      assert(recover(1, 2) != Running),
      assert(recover(2, 2) != Running),
    }
  }

  // A SCHEDULED job is promoted only once due; the runner claims nothing when
  // disabled.
  run scheduleAndRunnerGates = {
    all {
      assert(promotes(true)),
      assert(not(promotes(false))),
      assert(sweeps(true)),
      assert(not(sweeps(false))),
    }
  }

  // Invariant: a terminal status is a fixed point of every transition rule.
  val terminalIsFixedPoint = and {
    transition(Completed, Pending) == Completed,
    transition(Error, Running) == Error,
    transition(Cancelled, Pending) == Cancelled,
    complete(Completed, true, Running) == Completed,
    complete(Error, true, Completed) == Error,
  }
}
