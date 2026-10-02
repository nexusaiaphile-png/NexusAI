"""Windows Service wrapper for NexusAI Security Box."""
import servicemanager
import win32event
import win32service
import win32serviceutil

from edge_agent import security_box


class NexusAISecurityBoxService(win32serviceutil.ServiceFramework):
    _svc_name_ = "NexusAISecurityBox"
    _svc_display_name_ = "NexusAI Security Box"
    _svc_description_ = "NexusAI always-on local CCTV security service."

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogInfoMsg("NexusAI Security Box started.")
        security_box.run()


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(NexusAISecurityBoxService)
