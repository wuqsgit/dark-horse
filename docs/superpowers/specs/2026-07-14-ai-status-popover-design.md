# AI Status Popover Design

## Goal

Turn the compact AI status chip into a hover/focus popover that explains what the AI service is doing and shows current progress without adding a new page.

## Data

The AI status response exposes, for both `normal` and `alpha`, total samples, pending samples, labeled samples, required samples, model state/version, and today's decision counters. Maintenance timestamps and errors remain in the existing `maintenance` object.

## Interaction

Desktop shows the panel on hover or keyboard focus. Clicking pins/unpins it for touch devices. The panel contains a four-step workflow, per-model progress bars, today's allow/probe/reject counts, UTC+8 maintenance times, and any current error. It closes on mouse leave when not pinned and on Escape.

## Scope

No model thresholds, training rules, Trader behavior, or order execution behavior changes.
