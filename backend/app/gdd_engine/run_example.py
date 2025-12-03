from orchestrator.orchestrator import GDDOrchestrator

def main():
    # Define your game concept here
    concept = "Dota 2: Dawn of War — 2v2 auto-battler with hero fusion mechanics."

    print("\n🚀 Running Multi-Persona GDD Orchestration Pipeline...")
    print("=======================================================\n")

    engine = GDDOrchestrator(concept)

    try:
        result = engine.run_pipeline()
    except Exception as e:
        print("\n❌ Pipeline Error:")
        print(e)
        return

    print("\n======================= 📘 FINAL GDD MARKDOWN =======================\n")
    print(result["integration"]["markdown"])

    print("\n========================= 🧪 REVIEWER REPORT =========================\n")
    print(result["reviewer"])

    print("\n======================================================================")
    print("✔ Pipeline completed successfully!\n")

if __name__ == "__main__":
    main()
