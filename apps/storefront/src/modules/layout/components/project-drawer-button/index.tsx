import { retrieveCurrentProjectDraft } from "@lib/data/rfq"

import ProjectDrawerDropdown from "../project-drawer-dropdown"

export default async function ProjectDrawerButton() {
  const draft = await retrieveCurrentProjectDraft().catch(() => null)

  return <ProjectDrawerDropdown draft={draft} />
}

