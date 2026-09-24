"""Interactive call integration: HTTP input, core state machine, WS output, audit."""
from fastapi.testclient import TestClient
from api.app import create_app


def test_interactive_call_confirmation_requires_matching_pending_action():
    with TestClient(create_app()) as client:
        with client.websocket_connect('/ws/call/case1') as ws:
            response = client.post('/calls/case1/turn', json={'text': 'My car broke down on Highway 5 at mile 12. Need a tow.'})
            assert response.status_code == 200, response.text
            body = response.json()
            count = len(client.app.state.live_calls.sessions['case1'].history)
            received = [ws.receive_json() for _ in range(count)]
            kinds = [event['event_type'] for event in received]
            assert 'FinalTranscript' in kinds
            assert 'DeliberationResolved' in kinds
            assert 'ActionPending' in kinds
            assert 'ActionFinalized' not in kinds
            action = body['pending_action_id']
            assert action
            bad = client.post('/calls/case1/decision', json={'action_id': 'other', 'confirm': True})
            assert bad.status_code == 409
            good = client.post('/calls/case1/decision', json={'action_id': action, 'confirm': True})
            assert good.status_code == 200
            assert good.json()['state'] == 'finalized'
            assert ws.receive_json()['event_type'] == 'ActionFinalized'
            assert client.post('/calls/case1/decision', json={'action_id': action, 'confirm': True}).status_code == 409
            audit = client.get('/calls/case1/audit').json()
            assert audit['actions'][0]['current_state'] == 'finalized'
            assert audit['deliberations'][0]['decision_id'] == body['decision_id']


def test_new_information_aborts_pending_and_end_resolves_actions():
    with TestClient(create_app()) as client:
        first = client.post('/calls/case2/turn', json={'text': 'Flat tire on Highway 9 at mile 3'}).json()
        action = first['pending_action_id']
        assert action
        assert client.post('/calls/case2/interrupt').json() == {'interrupted': True}
        second = client.post('/calls/case2/turn', json={'text': 'Actually there is a fire on Highway 9 at mile 3'}).json()
        assert client.get('/calls/case2/audit').json()['actions'][0]['current_state'] == 'aborted'
        assert client.post('/calls/case2/decision', json={'action_id': action, 'confirm': True}).status_code == 409
        assert client.post('/calls/case2/end').json() == {'closed': True}
        audit = client.get('/calls/case2/audit').json()
        assert all(a['current_state'] != 'pending_confirmation' for a in audit['actions'])
        assert client.post('/calls/case2/turn', json={'text': 'hello'}).status_code == 409


def test_invalid_call_id_and_empty_turn_are_rejected():
    with TestClient(create_app()) as client:
        assert client.post('/calls/invalid.id/turn', json={'text': 'hello'}).status_code == 400
        assert client.post('/calls/valid/turn', json={'text': ''}).status_code == 422
