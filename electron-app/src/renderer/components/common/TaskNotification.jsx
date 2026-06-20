import { CheckCircle, AlertTriangle, X, Zap } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore.js";

export function TaskNotification() {
  const notifications = useSettingsStore((state) => state.taskNotifications);
  const dismissTaskNotification = useSettingsStore((state) => state.dismissTaskNotification);

  if (!notifications.length) {
    return null;
  }

  return (
    <div className="task-notification-container">
      {notifications.map((n) => {
        const isDone = n.status === "done";
        const Icon = isDone ? CheckCircle : AlertTriangle;
        const label = isDone ? "Completed" : "Partial";
        const snippet = n.notes
          ? n.notes.replace(/\*\*/g, "").replace(/#+\s/g, "").slice(0, 120) + (n.notes.length > 120 ? "..." : "")
          : "";
        return (
          <div key={n.id} className={`task-notification ${isDone ? "task-done" : "task-partial"}`}>
            <div className="task-notification-header">
              <Zap size={14} />
              <span className="task-notification-title">{n.title}</span>
              <button
                className="task-notification-dismiss"
                type="button"
                onClick={() => dismissTaskNotification(n.id)}
              >
                <X size={14} />
              </button>
            </div>
            <div className="task-notification-body">
              <Icon size={14} />
              <span>{label}</span>
            </div>
            {snippet ? (
              <div className="task-notification-notes">{snippet}</div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
