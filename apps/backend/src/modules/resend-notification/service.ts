import {
  AbstractNotificationProviderService,
  MedusaError,
} from "@medusajs/framework/utils"
import {
  Logger,
  ProviderSendNotificationDTO,
  ProviderSendNotificationResultsDTO,
} from "@medusajs/framework/types"

type ResendOptions = {
  api_key: string
  from: string
}

/** Resend (https://resend.com) email provider for Medusa's Notification module. */
class ResendNotificationService extends AbstractNotificationProviderService {
  static identifier = "notification-resend"

  protected options_: ResendOptions
  protected logger_: Logger

  constructor({ logger }: { logger: Logger }, options: ResendOptions) {
    super()
    this.options_ = options
    this.logger_ = logger
  }

  static validateOptions(options: Record<string, unknown>) {
    if (!options.api_key || !options.from) {
      throw new MedusaError(
        MedusaError.Types.INVALID_DATA,
        "The Resend provider requires `api_key` and `from` options"
      )
    }
  }

  async send(
    notification: ProviderSendNotificationDTO
  ): Promise<ProviderSendNotificationResultsDTO> {
    const providerData = (notification.provider_data ?? {}) as {
      reply_to?: string
      headers?: Record<string, string>
    }

    const response = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: {
        authorization: `Bearer ${this.options_.api_key}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        from: notification.from?.trim() || this.options_.from,
        to: [notification.to],
        subject: notification.content?.subject ?? notification.template,
        html: notification.content?.html,
        text: notification.content?.text,
        reply_to: providerData.reply_to,
        headers: providerData.headers,
      }),
    })

    const body = (await response.json().catch(() => ({}))) as {
      id?: string
      message?: string
    }
    if (!response.ok) {
      throw new MedusaError(
        MedusaError.Types.UNEXPECTED_STATE,
        `Resend rejected the email (${response.status}): ${body.message ?? "unknown error"}`
      )
    }

    return { id: body.id }
  }
}

export default ResendNotificationService
