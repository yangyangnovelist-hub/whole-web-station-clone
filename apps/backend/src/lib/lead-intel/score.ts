import { emailDomain, isFreemailDomain, PageSnapshot } from "./extract"
import { TrailSummary } from "./trail"

export type LeadGrade = "A" | "B" | "C" | "D"

export type LeadScoreInput = {
  email: string
  company?: string | null
  phone?: string | null
  quote_total?: number | null
  rush?: boolean
  trail?: TrailSummary | null
  site?: PageSnapshot | null
}

export function gradeForScore(score: number): LeadGrade {
  if (score >= 75) {
    return "A"
  }
  if (score >= 55) {
    return "B"
  }
  if (score >= 35) {
    return "C"
  }
  return "D"
}

/** Deterministic score used when AI is off, and as a floor for AI output. */
export function heuristicLeadScore(input: LeadScoreInput) {
  const reasons: string[] = []
  let score = 10

  if (!isFreemailDomain(emailDomain(input.email))) {
    score += 15
    reasons.push("Business email domain")
  }
  if (input.company?.trim()) {
    score += 10
    reasons.push("Company name provided")
  }
  if (input.phone?.trim()) {
    score += 5
    reasons.push("Phone number provided")
  }
  if (input.site) {
    score += 10
    reasons.push("Company website found")
  }

  const total = input.quote_total ?? 0
  if (total >= 10000) {
    score += 25
    reasons.push("Quote value above $10k")
  } else if (total >= 3000) {
    score += 18
    reasons.push("Quote value above $3k")
  } else if (total >= 1000) {
    score += 10
    reasons.push("Quote value above $1k")
  } else if (total > 0) {
    score += 4
  }

  if (input.rush) {
    score += 8
    reasons.push("Rush timeline requested")
  }

  const trail = input.trail
  if (trail) {
    if (trail.pages_viewed >= 8) {
      score += 10
      reasons.push(`Browsed ${trail.pages_viewed} pages`)
    } else if (trail.pages_viewed >= 3) {
      score += 6
      reasons.push(`Browsed ${trail.pages_viewed} pages`)
    }
    if (trail.quote_interactions >= 3) {
      score += 7
      reasons.push("Compared several quote configurations")
    }
    if (trail.active_minutes >= 5) {
      score += 4
    }
  }

  const bounded = Math.min(100, score)
  return { score: bounded, grade: gradeForScore(bounded), reasons }
}
