import { Modal } from "../primitives";
import { useStore } from "../state/store";
import { NewAgentModal } from "./modals/NewAgentModal";

export function ModalHost() {
  const { state, dispatch } = useStore();
  if (!state.modal) return null;
  const close = () => dispatch({ type: "closeModal" });
  return (
    <Modal onClose={close}>
      {state.modal === "new-agent" ? <NewAgentModal /> : null}
    </Modal>
  );
}
