"""NexusAI Security Box entry point.

This wrapper runs the existing NexusAI Edge Agent as a managed background
process. Service managers on Windows and macOS keep it alive across reboots.
"""
import edge_agent.agent as agent

if __name__ == "__main__":
    agent.main()
