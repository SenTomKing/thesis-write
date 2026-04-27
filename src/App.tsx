import React from 'react';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { Dashboard } from './pages/Dashboard';
import { CreateProject } from './pages/CreateProject';
import { Diagnostics } from './pages/Diagnostics';
import { Editor } from './pages/Editor';
import { Literature } from './pages/Literature';
import { Trash } from './pages/Trash';

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/create" element={<CreateProject />} />
          <Route path="/trash" element={<Trash />} />
          <Route path="/diagnose/:id" element={<Diagnostics />} />
          <Route path="/literature/:id" element={<Literature />} />
        </Route>
        <Route path="/editor/:id" element={<Editor />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
