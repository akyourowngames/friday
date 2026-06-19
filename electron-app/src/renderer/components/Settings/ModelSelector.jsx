import { Cpu } from "lucide-react";
import { KNOWN_MODELS, useSettingsStore } from "../../stores/settingsStore.js";

export function ModelSelector({ onSetModel }) {
  const model = useSettingsStore((state) => state.model);

  return (
    <label className="field">
      <span>
        <Cpu size={15} />
        Model
      </span>
      <select value={model} onChange={(event) => onSetModel(event.target.value)}>
        {KNOWN_MODELS.map((item) => (
          <option value={item} key={item}>
            {item}
          </option>
        ))}
      </select>
    </label>
  );
}
