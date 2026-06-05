"use server"

import { submitRfq } from "@lib/data/rfq"

export type ContactRfqFormState = {
  success: boolean
  message: string | null
  rfqId?: string | null
}

const INITIAL_STATE: ContactRfqFormState = {
  success: false,
  message: null,
  rfqId: null,
}

const timeFrameLabels: Record<string, string> = {
  less_than_1_month: "Less than 1 month",
  one_to_three_months: "1 - 3 months",
  more_than_3_months: "More than 3 months",
}

function getField(formData: FormData, key: string) {
  return formData.get(key)?.toString().trim() || ""
}

function normalizeQuantity(rawValue: string) {
  const quantity = Number.parseInt(rawValue, 10)

  if (!Number.isFinite(quantity) || quantity <= 0) {
    return null
  }

  return quantity
}

export async function submitContactRfq(
  _: ContactRfqFormState,
  formData: FormData
): Promise<ContactRfqFormState> {
  const firstName = getField(formData, "first_name")
  const lastName = getField(formData, "last_name")
  const email = getField(formData, "email")
  const phone = getField(formData, "phone")
  const company = getField(formData, "company")
  const product = getField(formData, "product")
  const quantity = normalizeQuantity(getField(formData, "quantity"))
  const timeFrame = getField(formData, "time_frame")
  const projectDescription = getField(formData, "project_description")

  if (!firstName || !lastName || !email || !phone || !product || !quantity) {
    return {
      ...INITIAL_STATE,
      message:
        "Please complete the required contact and project details before submitting.",
    }
  }

  if (!projectDescription) {
    return {
      ...INITIAL_STATE,
      message: "Please describe your packaging project so the team can review it.",
    }
  }

  const contactName = `${firstName} ${lastName}`.trim()
  const timeFrameLabel = timeFrameLabels[timeFrame]

  const notes = [
    timeFrameLabel ? `Requested time frame: ${timeFrameLabel}` : null,
    projectDescription,
  ]
    .filter(Boolean)
    .join("\n\n")

  try {
    const rfq = await submitRfq({
      source: "contact_form",
      contact_name: contactName,
      email,
      company,
      phone,
      title: `${product} RFQ`,
      application: product,
      quantity,
      notes,
      items: [
        {
          title: product,
          quantity,
          notes: projectDescription,
          metadata: timeFrameLabel
            ? {
                time_frame: timeFrameLabel,
              }
            : undefined,
        },
      ],
    })

    return {
      success: true,
      message:
        "Your request has been submitted. A PackOasis specialist will follow up shortly.",
      rfqId: rfq?.id || null,
    }
  } catch (error) {
    return {
      ...INITIAL_STATE,
      message:
        error instanceof Error
          ? error.message
          : "We could not submit your request right now. Please try again.",
    }
  }
}

