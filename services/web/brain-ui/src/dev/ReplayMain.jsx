// Dev-only entry. Vite's production build has only index.html as an input.
import { createRoot } from 'react-dom/client'
import { DashboardView } from '../components/Dashboard'
import { fixtureStore } from './fixtures'
import '../App.css'
createRoot(document.getElementById('root')).render(<DashboardView store={fixtureStore()} demo onLogout={() => { location.href = '/' }} />)
