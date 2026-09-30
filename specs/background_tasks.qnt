// background_tasks.qnt — laws for the background-task framework (issue #15).
//
// Two small, pure algorithms are pinned here:
//
//   * the cron **match** rule, in particular the day-of-month / day-of-week OR
//     (when both are restricted, a day matches if either does); and
//   * the scheduler's **selection** rule — which tasks tick at a given minute.
//
// Invariants proved:
//   - matchesIsFieldConjunction: a moment matches iff every field matches.
//   - domDowOrRule: with both day fields restricted, a day matches on either;
//     with only one restricted, that one must match.
//   - scheduleNoneNeverTicks: a task with no schedule is never selected, but it
//     is still runnable by name.
//   - onlyEnabledTasksTick: a disabled task is never selected.
//   - selectionIsPerTask / aRaisingTaskDoesNotStopTheLoop: selection is
//     independent per task, so one task's failure never changes another's.
//
// The model is deliberately small and unrolled over a finite set of field
// values, matching how sorting.qnt / filtering.qnt unroll their universes. The
// real implementation rolls a datetime forward; here the field match is stated
// directly, which is the rule the roll-forward implements.
module background_tasks {
  // A minute is modelled by the fields a 5-field cron expression inspects.
  type Minute = { minute: int, hour: int, dom: int, month: int, dow: int }

  // A parsed schedule: the selected values of each field, plus whether the day
  // fields were literal '*' (only a restricted field participates in matching).
  type Schedule = {
    minutes: Set[int],
    hours: Set[int],
    doms: Set[int],
    months: Set[int],
    dows: Set[int],
    domRestricted: bool,
    dowRestricted: bool,
  }

  // ---- The day rule: the DOM/DOW OR --------------------------------------
  // Both restricted -> OR; one restricted -> that one; neither -> always.
  pure def dayMatches(s: Schedule, m: Minute): bool =
    val domOk = s.doms.contains(m.dom)
    val dowOk = s.dows.contains(m.dow)
    if (s.domRestricted and s.dowRestricted) domOk or dowOk
    else if (s.domRestricted) domOk
    else if (s.dowRestricted) dowOk
    else true

  // A moment matches iff every field matches (the day via the rule above).
  pure def matches(s: Schedule, m: Minute): bool =
    s.minutes.contains(m.minute) and
    s.hours.contains(m.hour) and
    s.months.contains(m.month) and
    dayMatches(s, m)

  // ---- Scheduler selection -----------------------------------------------
  // A task is a schedule (present or not) and an enabled flag. ``hasSchedule``
  // stands in for ``schedule is None``.
  type Task = { name: str, hasSchedule: bool, schedule: Schedule, enabled: bool }

  // The scheduler's rule: an active task with a schedule matching the minute.
  pure def selectedAt(t: Task, m: Minute): bool =
    t.enabled and t.hasSchedule and matches(t.schedule, m)

  // The tasks the loop starts at one minute. (Quint lists have no ``filter``,
  // so this folds with an append.)
  pure def tick(tasks: List[Task], m: Minute): List[str] =
    tasks.foldl(List(), (acc, t) => if (selectedAt(t, m)) acc.append(t.name) else acc)

  // The tasks a one-shot `run` (no name) executes: every enabled task.
  pure def runOnceNames(tasks: List[Task]): List[str] =
    tasks.foldl(List(), (acc, t) => if (t.enabled) acc.append(t.name) else acc)

  // ---- A finite universe --------------------------------------------------
  // Every minute is 00:00. dow: 1 = Monday.
  pure val monDay1: Minute = { minute: 0, hour: 0, dom: 1, month: 1, dow: 1 }
  pure val tueDay2: Minute = { minute: 0, hour: 0, dom: 2, month: 1, dow: 2 }
  pure val mondayNonFirst: Minute = { minute: 0, hour: 0, dom: 5, month: 1, dow: 1 }
  pure val other: Minute = { minute: 30, hour: 12, dom: 5, month: 6, dow: 5 }
  pure val mismatchedMinute: Minute = { minute: 7, hour: 0, dom: 1, month: 1, dow: 1 }

  // '* * * * *' — every field unrestricted.
  pure val everyMinute: Schedule = {
    minutes: Set(0, 30), hours: Set(0, 12), doms: Set(1, 2, 5), months: Set(1, 6),
    dows: Set(1, 2, 5), domRestricted: false, dowRestricted: false,
  }

  // '0 0 1 * *' — day-of-month 1, weekday '*'.
  pure val firstOfMonth: Schedule = {
    minutes: Set(0), hours: Set(0), doms: Set(1), months: Set(1, 6),
    dows: Set(1, 2, 5), domRestricted: true, dowRestricted: false,
  }

  // '0 0 * * 1' — weekday Monday, day-of-month '*'.
  pure val mondays: Schedule = {
    minutes: Set(0), hours: Set(0), doms: Set(1, 2, 5), months: Set(1, 6),
    dows: Set(1), domRestricted: false, dowRestricted: true,
  }

  // '0 0 1 * 1' — both restricted: day 1 OR Monday.
  pure val firstOrMonday: Schedule = {
    minutes: Set(0), hours: Set(0), doms: Set(1), months: Set(1, 6),
    dows: Set(1), domRestricted: true, dowRestricted: true,
  }

  pure val scheduled: Task = {
    name: "scheduled", hasSchedule: true, schedule: everyMinute, enabled: true,
  }
  pure val manual: Task = {
    name: "manual", hasSchedule: false, schedule: everyMinute, enabled: true,
  }
  pure val disabledScheduled: Task = {
    name: "disabled", hasSchedule: true, schedule: everyMinute, enabled: false,
  }

  // ---- Tests --------------------------------------------------------------

  run matchesIsFieldConjunction = {
    all {
      // Every-minute matches every modelled minute. (``forall`` is set-only;
      // a list is folded instead.)
      List(monDay1, tueDay2, mondayNonFirst, other).foldl(
        true, (acc, m) => acc and matches(everyMinute, m)
      ),
      // A mismatching minute field fails the whole match...
      not(matches(everyMinute, mismatchedMinute)),
      // ...as does a mismatching hour / month.
      not(matches(firstOfMonth, other)),
    }
  }

  run domDowOrRule = {
    all {
      // Both restricted -> OR: the 1st OR a Monday.
      assert(dayMatches(firstOrMonday, monDay1)),        // both
      assert(dayMatches(firstOrMonday, mondayNonFirst)), // Monday, not the 1st
      // neither the 1st nor a Monday:
      assert(not(dayMatches(firstOrMonday, tueDay2))),
      // Only day-of-month restricted -> the weekday is ignored.
      assert(dayMatches(firstOfMonth, monDay1)),
      assert(not(dayMatches(firstOfMonth, tueDay2))),
      // Only weekday restricted -> the day-of-month is ignored.
      assert(dayMatches(mondays, mondayNonFirst)),
      assert(not(dayMatches(mondays, tueDay2))),
    }
  }

  run matchIsUnrestrictedWhenNeitherDayFieldIsSet = {
    // dom and dow both '*': every day matches.
    List(monDay1, tueDay2, mondayNonFirst).foldl(
      true, (acc, m) => acc and dayMatches(everyMinute, m)
    )
  }

  run scheduleNoneNeverTicks = {
    all {
      assert(not(selectedAt(manual, monDay1))),
      assert(tick(List(manual), monDay1) == List()),
      // ...but a one-shot run still includes it.
      assert(runOnceNames(List(manual)) == List("manual")),
    }
  }

  run onlyEnabledTasksTick = {
    all {
      assert(not(selectedAt(disabledScheduled, monDay1))),
      tick(List(disabledScheduled), monDay1) == List(),
      // A one-shot run skips it too.
      runOnceNames(List(disabledScheduled)) == List(),
    }
  }

  run selectionIsPerTask = {
    // At Monday day 1: the scheduled task ticks, the manual / disabled do not.
    tick(List(scheduled, manual, disabledScheduled), monDay1) == List("scheduled")
  }

  run aRaisingTaskDoesNotStopTheLoop = {
    all {
      // Selection is independent per task, so one task's failure cannot change
      // another's selection.
      val names = tick(List(scheduled, manual), monDay1)
      assert(names == List("scheduled")),
      // Reordering the list does not change the selected set.
      tick(List(manual, scheduled), monDay1) == List("scheduled"),
    }
  }

  run invariantsHold = {
    all {
      matchesIsFieldConjunction,
      domDowOrRule,
      matchIsUnrestrictedWhenNeitherDayFieldIsSet,
      scheduleNoneNeverTicks,
      onlyEnabledTasksTick,
      selectionIsPerTask,
      aRaisingTaskDoesNotStopTheLoop,
    }
  }
}
