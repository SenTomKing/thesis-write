import React, { useEffect } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';

export const Diagnostics: React.FC = () => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    if (!id) {
      navigate('/', { replace: true });
      return;
    }
    navigate(`/editor/${id}`, { replace: true, state: location.state });
  }, [id, location.state, navigate]);

  return (
    <div className="loading-state">
      <div className="spinner"></div>
      <p>正在跳转到编辑器...</p>
    </div>
  );
};
