"""Audited, role-aware preparation of a lock-in pair from validated TOML."""
from dataclasses import asdict
from datetime import datetime, timezone
import math
from pathlib import Path

from .electrical_lockin_backend import native_status_problems
from .lockin_model_settings import model_name, receiver_invariant_targets, sensitivity_full_scale_for
from .sr830 import Sr830Error, verify_fixed_settings_readback


def _status_problems(diagnostic, *, frequency_transition=False, time_constant_transition=False):
    role = 'lockin_' + diagnostic.role.value
    status = diagnostic.lia_status
    if status is None or diagnostic.error_status is None:
        return [f'{role}: incomplete safety status']
    problems = []
    native = model_name(diagnostic) == 'SR865A'
    if native:
        problems.extend(native_status_problems(status))
    elif status.triggered or status.raw & ~0x7F:
        problems.append('unexpected trigger/unknown status latch')
    if diagnostic.error_status:
        problems.append(f'instrument error {diagnostic.error_status}')
    # Only a recorded XY transition latch may clear once. A currently unlocked
    # native receiver, unknown status or device error always blocks acceptance.
    if status.reference_unlocked and not (
        frequency_transition and diagnostic.role.value == 'xy'
        and (not native or diagnostic.native_status.locked is True)
    ):
        problems.append('reference unlocked')
    if status.input_or_reserve_overload or status.filter_overload or (native and status.output_overload):
        problems.append('overload')
    if status.frequency_range_changed and not frequency_transition:
        problems.append('unexpected frequency-change latch')
    if status.time_constant_changed and not time_constant_transition:
        problems.append('unexpected time-constant-change latch')
    return [f'{role}: {problem}' for problem in problems]


def apply_pair_toml(args, settings, factory):
    # Lazy import: single-role CLI behavior stays in its existing implementation.
    from . import lockin_test as daily

    config = settings['config']
    targets = {'lockin_xx': config.lockin_xx, 'lockin_xy': config.lockin_xy}
    codes = {role: daily._setting_codes(target) for role, target in targets.items()}
    amplitude = config.lockin_xx.source_voltage_v
    frequency = config.lockin_xx.frequency_hz
    safety = daily._validate_frequency_source_safety(config, amplitude)
    daily._validate_frequency_observations(frequency)
    if config.lockin_xy.frequency_hz != frequency:
        raise ValueError('Pair apply requires matching XX/XY TOML reference frequencies.')
    settle_s = max(1.5, config.lockin_sweep.settle_time_constants * max(
        target.time_constant_s for target in targets.values()))
    directory = daily._prepare_sweep_record_directory(args, settings)
    record = {
        'schema_version': 1, 'scan': 'apply_toml', 'command': 'apply-toml',
        'captured_at_utc': datetime.now(timezone.utc).isoformat(),
        'captured_unix_s': daily.time.time(), 'completed': False, 'outcome': 'rejected',
        'target_roles': list(targets), 'run_metadata': daily._sweep_run_metadata(settings),
        'config_source': str(Path(args.config).resolve()),
        'lockin_safety_sha256': daily._lockin_safety_sha256(settings),
        'resolved_target_config': {role: asdict(target) for role, target in targets.items()},
        'source_safety_nominal_estimates': safety, 'settle_s': settle_s,
        'requested': {'xx_source_voltage_v': amplitude, 'frequency_hz': frequency, 'harmonic': 1},
        'status_latches_consumed': True, 'write_performed': False,
        'communication_uncertain': False, 'manual_verification_required': False,
        'actions': [], 'last_confirmed_state': {},
        'never_written': ['PHAS', 'XY SLVL/SOFF/REFM/BLAZEX/FREQINT'],
        'cleanup': {'attempted': False, 'policy': 'On known communication, verify XX 4 mVrms; never restore prior higher excitation.'},
    }
    changed = {role: set() for role in targets}

    def call(role, method, *values, write=False, **kwargs):
        action = {'role': role, 'method': method, 'arguments': list(values),
                  'keyword_arguments': kwargs, 'write_attempted': write,
                  'started_at_utc': datetime.now(timezone.utc).isoformat()}
        record['actions'].append(action)
        if write:
            record['write_performed'] = True
        try:
            result = getattr(instruments[role], method)(*values, **kwargs)
            action['result'] = result
            return result
        except BaseException as exc:
            # A failed instrument operation may include a partial write/read.
            # No additional commands are safe until independently verified.
            record['communication_uncertain'] = True
            action['error'] = repr(exc)
            raise
        finally:
            action['finished_at_utc'] = datetime.now(timezone.utc).isoformat()

    def change(role, label, target, reader, writer, *, field=None, tolerance=None):
        read_args = () if field is None else (field,)
        actual = call(role, reader, *read_args)
        matches = lambda value: value == target if tolerance is None else math.isclose(
            value, target, rel_tol=0., abs_tol=tolerance)
        if matches(actual):
            return
        changed[role].add(label)
        call(role, writer, *read_args, target, write=True)
        actual = call(role, reader, *read_args)
        record['last_confirmed_state'][role + '.' + label] = actual
        if not matches(actual):
            raise Sr830Error(f'{role} {label}: requested {target!r}, actual {actual!r}'
                             + ('' if tolerance is None else f', tolerance {tolerance:g}'))

    def fixed(role, field, target):
        change(role, field, target, 'read_fixed_setting', 'set_fixed_setting', field=field)

    def snapshot(label, *, transition=False):
        result = {}
        record[label] = {}
        for role in targets:
            diagnostic = call(role, 'read_diagnostic', consume_status_latches=True)
            result[role] = diagnostic
            record[label][role] = asdict(diagnostic)
            record['last_confirmed_state'][role] = record[label][role]
        if result['lockin_xx'].identity == result['lockin_xy'].identity:
            raise Sr830Error('Both addresses returned the same instrument identity.')
        frequency_changed = any(fields & {'reference_mode', 'reference_slope', 'harmonic', 'frequency'}
                                for fields in changed.values())
        problems = []
        for role, diagnostic in result.items():
            problems.extend(_status_problems(diagnostic,
                frequency_transition=transition and frequency_changed,
                time_constant_transition=transition and 'time_constant' in changed[role]))
        if problems:
            raise Sr830Error(f'Pair apply {label}: ' + '; '.join(problems))
        return result

    def verify(actual, expected_amplitude):
        for role, diagnostic in actual.items():
            verify_fixed_settings_readback(diagnostic, codes[role], before[role].phase_shift_deg)
            reserve = daily.reserve_code_for(targets[role])
            if diagnostic.reserve_mode != reserve:
                raise Sr830Error(f'{role} Reserve: requested {reserve}, actual {diagnostic.reserve_mode}')
            if diagnostic.harmonic != 1:
                raise Sr830Error(f'{role} harmonic: requested 1, actual {diagnostic.harmonic}')
        xx, xy = actual.values()
        if not math.isclose(xx.sine_output_v, expected_amplitude, rel_tol=0., abs_tol=.0005):
            raise Sr830Error(f'lockin_xx output: requested {expected_amplitude:g}, actual {xx.sine_output_v:g} V RMS')
        daily._validate_frequency_source_safety(config, xx.sine_output_v)
        daily._verify_requested_sweep_frequency_readbacks(frequency, xx.frequency_hz, xy.frequency_hz)
        daily._verify_requested_sweep_frequency_readbacks(frequency, xx.snapshot_frequency_hz, xy.snapshot_frequency_hz)
        if model_name(xy) == 'SR865A':
            native = call('lockin_xy', 'read_receiver_invariants')
            record['receiver_invariants'] = native
            for field, expected in receiver_invariant_targets(config.lockin_xy).items():
                if native.get(field) != expected:
                    raise Sr830Error(f'lockin_xy {field}: requested {expected!r}, actual {native.get(field)!r}')
        elif xy.sine_output_v != before['lockin_xy'].sine_output_v:
            raise Sr830Error('Disconnected XY source output changed.')

    try:
        record['communication_uncertain'] = True
        with daily._open_pair(settings, factory) as pair:
            record['communication_uncertain'] = False
            instruments = dict(zip(targets, pair))
            try:
                before = snapshot('before')
                # Accept valid configuration differences, never malformed/unknown state.
                for role, diagnostic in before.items():
                    daily._validate_frequency_observations(diagnostic.frequency_hz, diagnostic.snapshot_frequency_hz)
                    if not math.isfinite(diagnostic.sine_output_v) or (
                        model_name(diagnostic) == 'SR830' and not .004 <= diagnostic.sine_output_v <= 5.
                    ):
                        raise Sr830Error(f'{role}: invalid source amplitude readback')
                    for field in ('reference_mode', 'reference_slope', *daily._SWEEP_FIXED_SETTING_FIELDS):
                        call(role, 'read_fixed_setting', field)
                change('lockin_xx', 'source_voltage', .004, 'read_sine_output', 'set_sine_output', tolerance=.0005)
                for role, target in targets.items():
                    if model_name(target) == 'SR865A':
                        native_before = before[role].native_settings
                        native_targets = receiver_invariant_targets(target)
                        pending = any(getattr(native_before, key) != native_targets[key] for key in (
                            'reference_source', 'external_reference_edge', 'reference_input_impedance_ohm',
                            'input_range_v_peak', 'advanced_filter', 'synchronous_filter'))
                        if pending:
                            changed[role].add('receiver_settings')
                        if native_before.reference_source != native_targets['reference_source']:
                            changed[role].add('reference_mode')
                        if native_before.external_reference_edge != native_targets['external_reference_edge']:
                            changed[role].add('reference_slope')
                        record['receiver_setup'] = call(role, 'configure_receiver', write=pending)
                    else:
                        fixed(role, 'reference_mode', codes[role].reference_source)
                        if role == 'lockin_xy':
                            fixed(role, 'reference_slope', codes[role].external_reference_edge)
                    change(role, 'harmonic', 1, 'read_harmonic', 'set_harmonic')
                    for field in daily._SWEEP_FIXED_SETTING_FIELDS:
                        fixed(role, field, getattr(codes[role], field))
                    old_sensitivity = call(role, 'read_sensitivity')
                    target_sensitivity = codes[role].sensitivity
                    widening = sensitivity_full_scale_for(target, target_sensitivity) > sensitivity_full_scale_for(target, old_sensitivity)
                    if widening:
                        change(role, 'sensitivity', target_sensitivity, 'read_sensitivity', 'set_sensitivity')
                    reserve = daily.reserve_code_for(target)
                    if reserve is not None:
                        change(role, 'reserve', reserve, 'read_reserve_mode', 'set_reserve_mode')
                    if not widening:
                        change(role, 'sensitivity', target_sensitivity, 'read_sensitivity', 'set_sensitivity')
                change('lockin_xx', 'frequency', frequency, 'read_reference_frequency',
                       'set_internal_reference_frequency', tolerance=daily._sweep_requested_frequency_tolerance_hz(frequency))
                if any(changed.values()):
                    daily.time.sleep(settle_s)
                    snapshot('transition', transition=True)
                    daily.time.sleep(settle_s)
                verify(snapshot('prepared'), .004)
                change('lockin_xx', 'source_voltage', amplitude, 'read_sine_output', 'set_sine_output', tolerance=.0005)
                if amplitude != .004:
                    daily.time.sleep(settle_s)
                verify(snapshot('after'), amplitude)
                record['completed'] = True
                record['outcome'] = 'completed'
            except BaseException:
                if record['write_performed'] and not record['communication_uncertain']:
                    cleanup = record['cleanup']
                    cleanup['attempted'] = True
                    try:
                        change('lockin_xx', 'cleanup_source_voltage', .004, 'read_sine_output', 'set_sine_output', tolerance=.0005)
                        cleanup['actual_source_voltage_v'] = call('lockin_xx', 'read_sine_output')
                        cleanup['minimum_output_verified'] = math.isclose(cleanup['actual_source_voltage_v'], .004, rel_tol=0., abs_tol=.0005)
                    except BaseException as cleanup_error:
                        cleanup['error'] = repr(cleanup_error)
                        cleanup['minimum_output_verified'] = False
                raise
            finally:
                if model_name(config.lockin_xy) == 'SR865A':
                    record['native_audit'] = instruments['lockin_xy'].audit
    except BaseException as exc:
        if record['completed']:  # Closing a resource also failed; do not certify success.
            record['communication_uncertain'] = True
        record['error'] = str(exc)
        record.update(daily._native_error_audit(exc))
        record['completed'] = False
        record['outcome'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'rejected'
        record['manual_verification_required'] = True
        daily._emit_sweep_result(directory, record)
        raise
    daily._emit_sweep_result(directory, record)
    return 0
