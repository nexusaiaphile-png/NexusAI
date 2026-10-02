"""NexusAI Security Box entry point.

This wrapper runs the existing hardened NexusAI Edge Agent under a platform
service manager. The service manager handles startup and crash recovery.
"""
import edge_agent.agent as agent

def run():
    return agent.main()

if __name__ == "__main__":
    run()
