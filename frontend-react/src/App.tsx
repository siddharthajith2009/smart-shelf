import DemoOne from "@/components/demo";
import ScrollElementDemo from "@/components/scroll-element-demo";

function App() {
  return (
    <main className="min-h-screen bg-background">
      <div className="flex items-center justify-center p-6 py-16">
        <DemoOne />
      </div>
      <ScrollElementDemo />
    </main>
  );
}

export default App;
