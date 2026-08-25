# A cheap tripwire. This stack should cost nothing; any real spend means
# something is wrong (an image repo filling up, min_instance_count left at 1,
# someone hammering the public endpoint), and you want to hear about it early.
#
# Skipped entirely when billing_account_id is empty -- creating a budget needs
# permission on the *billing account*, which is separate from project Owner.
resource "google_billing_budget" "guardrail" {
  provider = google.billing
  count    = var.billing_account_id != "" ? 1 : 0

  billing_account = var.billing_account_id
  display_name    = "${var.function_name} (${var.project_id})"

  budget_filter {
    projects        = ["projects/${data.google_project.this.number}"]
    calendar_period = "MONTH"

    # Include every credit type *except* PROMOTION, which is what the $300
    # free-trial grant is. With INCLUDE_ALL_CREDITS the trial credit cancels
    # out the cost and reported spend sits at $0 for 90 days, so the alert
    # would stay silent however badly things were going. This way the budget
    # tracks what you'd actually be paying once the trial ends.
    #
    # FREE_TIER has to stay in the list: free-tier usage is billed at list
    # price and then credited back, so dropping it would trip a $1 budget on
    # completely normal traffic.
    credit_types_treatment = "INCLUDE_SPECIFIED_CREDITS"
    credit_types = [
      "FREE_TIER",
      "SUSTAINED_USAGE_DISCOUNT",
      "COMMITTED_USAGE_DISCOUNT",
      "DISCOUNT",
    ]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.budget_amount)
    }
  }

  # Actual spend. 50% of a $1 budget is 50 cents -- early enough to catch a
  # slow leak before it's a real bill.
  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  # And a heads-up if the month is merely trending over.
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  # With no all_updates_rule, Google emails the billing account's admins and
  # users -- which is you, and needs no extra wiring.

  depends_on = [google_project_service.apis]
}
