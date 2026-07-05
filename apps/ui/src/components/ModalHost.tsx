import { Modal } from "../primitives";
import { useStore } from "../state/store";
import { NewAgentModal } from "./modals/NewAgentModal";
import { PluginModal } from "./modals/PluginModal";
import { SlackModal } from "./modals/SlackModal";

export function ModalHost() {
  const { state, dispatch } = useStore();
  if (!state.modal) return null;
  const close = () => dispatch({ type: "closeModal" });
  return (
    <Modal onClose={close}>
      {state.modal === "new-agent" ? <NewAgentModal /> : null}
      {state.modal === "plugin" ? <PluginModal /> : null}
      {state.modal === "slack-oauth" ? <SlackModal /> : null}
    </Modal>
  );
}
